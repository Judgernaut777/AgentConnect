"""Hardening regressions for the work queue: the SHARED-connection concurrency
path the broker actually uses, add()'s dependency privacy check, the opt-in
reaper thread, and delivery-error surfacing.

test_workqueue.py's race test deliberately gives each thread its OWN connection
(the multi-process path, serialized by SQLite's file lock). The broker instead
serves remote workers from a thread pool over ONE shared connection — so these
tests share a single WorkQueue across threads, which is where an unsynchronized
commit/rollback would corrupt a peer's in-flight claim.
"""

import threading
import time

from agentconnect.common.config import load_routing
from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import PrivacyClass, WorkerResult
from agentconnect.common.workqueue import WorkQueue

LOCAL = "local_only"
EXTERNAL = "external"


def _wq():
    mem = SharedMemory()
    return WorkQueue(mem, load_routing()), mem


# ------------------------------------------------------ shared-connection races
def test_shared_connection_no_double_claim_under_contention():
    """Many threads, ONE WorkQueue/connection, fewer tickets than threads. Every
    ticket must be won by exactly one thread and incremented exactly once — no
    double-claim, no claim silently rolled back by a losing peer."""
    wq, _ = _wq()
    n_tickets = 12
    ids = [wq.add(privacy_class=PrivacyClass.public, payload=f"t{i}", origin="o")["ticket_id"]
           for i in range(n_tickets)]

    n_workers = 8
    barrier = threading.Barrier(n_workers)
    won: dict[str, list[str]] = {}
    lock = threading.Lock()

    def worker(name: str):
        barrier.wait()
        while True:
            got = wq.claim_next(name, LOCAL, max=1)
            if not got:
                break
            with lock:
                won.setdefault(got[0]["ticket_id"], []).append(name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one winner per ticket, and every ticket claimed.
    assert set(won) == set(ids), "some tickets never claimed or unknown id appeared"
    doubles = {tid: ws for tid, ws in won.items() if len(ws) != 1}
    assert not doubles, f"tickets claimed by more than one worker: {doubles}"
    for tid in ids:
        row = wq.get(tid)
        assert row["status"] == "claimed"
        assert row["attempts"] == 1, f"{tid} attempts={row['attempts']} (double-count)"


def test_shared_connection_claim_and_report_interleave():
    """Interleave claims (one thread) with reports (another) on the same
    connection; the store's writes (put_artifact commits) must not corrupt an
    in-flight claim. Assert every processed ticket ends terminal-and-consistent."""
    wq, _ = _wq()
    ids = [wq.add(privacy_class=PrivacyClass.public, payload=f"t{i}", origin="o")["ticket_id"]
           for i in range(20)]
    barrier = threading.Barrier(2)
    claimed: list[dict] = []
    clock = threading.Lock()

    def claimer():
        barrier.wait()
        while len(claimed) < len(ids):
            got = wq.claim_next("c", LOCAL, max=1)
            if got:
                with clock:
                    claimed.append(got[0])
            else:
                time.sleep(0.001)

    def reporter():
        barrier.wait()
        done = 0
        while done < len(ids):
            with clock:
                pending = claimed[done:]
            for t in pending:
                wq.report("c", LOCAL, t["ticket_id"], t["lease_token"],
                          WorkerResult(status="completed", summary="ok"))
                done += 1
            time.sleep(0.001)

    threads = [threading.Thread(target=claimer), threading.Thread(target=reporter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for tid in ids:
        row = wq.get(tid)
        assert row["status"] == "done", f"{tid} ended {row['status']}"
        assert row["result_status"] == "approved"


# --------------------------------------------------- add() dependency privacy
def test_add_rejects_privacy_downgrade_dependency():
    """A public (widely-claimable) child depending on a repo_sensitive parent is a
    laundering path: add() must refuse it, exactly as link() does."""
    wq, _ = _wq()
    parent = wq.add(privacy_class=PrivacyClass.repo_sensitive, payload="secret", origin="o")
    child = wq.add(privacy_class=PrivacyClass.public, payload="pub", origin="o",
                   depends_on=[parent["ticket_id"]])
    assert child == {"error": "privacy_downgrade", "depends_on": parent["ticket_id"]}


def test_add_allows_monotonic_dependency():
    """A repo_sensitive child (narrower) depending on a public parent (wider) is
    fine — the child is at least as restrictive."""
    wq, _ = _wq()
    parent = wq.add(privacy_class=PrivacyClass.public, payload="pub", origin="o")
    child = wq.add(privacy_class=PrivacyClass.repo_sensitive, payload="secret", origin="o",
                   depends_on=[parent["ticket_id"]])
    assert "ticket_id" in child


# ------------------------------------------------------------- reaper thread
def test_start_reaper_requeues_expired_lease():
    """The opt-in daemon reaper requeues a ticket whose lease expired — the
    self-healing a manual-only reaper never provided."""
    wq, _ = _wq()
    t = wq.add(privacy_class=PrivacyClass.public, payload="x", origin="o")["ticket_id"]
    # Claim with a zero-length lease so it is already expired.
    got = wq.claim("w", LOCAL, t, lease_seconds=0)
    assert got.get("ticket_id") == t
    assert wq.get(t)["status"] == "claimed"

    thread, stop = wq.start_reaper(interval=0.02)
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline and wq.get(t)["status"] != "open":
            time.sleep(0.02)
    finally:
        stop.set()
        thread.join(timeout=1.0)

    assert wq.get(t)["status"] == "open", "reaper thread did not requeue the expired lease"


def test_daemon_reaper_concurrent_with_claim_and_report_shared_connection():
    """Crown-jewel concurrency: the auto-started daemon reaper (what
    add_pull_routes mounts by default) runs reap_expired — requeue + park, two
    statements on the ONE shared connection — WHILE workers claim and report on
    that same connection. A regression dropping @_synchronized from reap_expired,
    requeuing at the same lease_token, or letting a report land on a
    reaper-requeued ticket would corrupt an in-flight claim/report or double-count
    attempts. With short leases some tickets ARE reaped mid-flight, exercising the
    fencing: a stale-token report after a requeue must be refused, and each ticket
    must still reach 'done' exactly once with attempts never exceeding the cap."""
    wq, _ = _wq()
    n = 40
    ids = [
        wq.add(privacy_class=PrivacyClass.public, payload=f"t{i}", origin="o",
               max_attempts=1000)["ticket_id"]
        for i in range(n)
    ]

    thread, stop = wq.start_reaper(interval=0.001)
    accepted: dict[str, int] = {}
    stale_lost = [0]
    alock = threading.Lock()

    def worker(name: str):
        while True:
            with alock:
                if len(accepted) >= n:
                    break
            got = wq.claim_next(name, LOCAL, max=1, lease_seconds=0.05)
            if not got:
                time.sleep(0.001)
                continue
            t = got[0]
            out = wq.report(name, LOCAL, t["ticket_id"], t["lease_token"],
                            WorkerResult(status="completed", summary="ok"))
            if out.get("ticket_status") == "done":
                with alock:
                    accepted[t["ticket_id"]] = accepted.get(t["ticket_id"], 0) + 1
            elif out.get("error") == "lease_lost":
                # The reaper requeued this ticket between our claim and report;
                # the stale token is correctly refused. Another claim will retry.
                with alock:
                    stale_lost[0] += 1

    try:
        workers = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=20)
    finally:
        stop.set()
        thread.join(timeout=1.0)

    # Exactly-once terminal state for every ticket; no double-accept.
    assert set(accepted) == set(ids), "some ticket never reached done under the reaper"
    assert all(v == 1 for v in accepted.values()), f"double-accepted: {accepted}"
    for tid in ids:
        row = wq.get(tid)
        assert row["status"] == "done", f"{tid} ended {row['status']}"
        assert row["result_status"] == "approved"
        # Monotonic, bounded attempts even across reaper requeues.
        assert 1 <= row["attempts"] <= row["max_attempts"], f"{tid} attempts={row['attempts']}"


# ------------------------------------------- interrupted-cascade self-healing
# report()/reject()/reap_expired() commit the terminal-failure transition and
# its dependent cascade in SEPARATE transactions. A store hiccup between them
# can leave a terminally-failed parent with its dependents un-cascaded — blocked
# forever, since a 'failed' parent can never satisfy the depends_on='done' claim
# gate. Each terminal path must therefore re-drive the idempotent cascade when
# re-invoked on an already-terminal parent that still has non-terminal children.

def _parent_child_stranded(wq):
    """Build a parent+child (child depends_on parent) and force the exact state a
    mid-cascade failure leaves: parent terminally 'failed', child still 'open'
    (never cascaded). Returns (parent_id, child_id)."""
    parent = wq.add(privacy_class=PrivacyClass.public, payload="p", origin="o")
    child = wq.add(privacy_class=PrivacyClass.public, payload="c", origin="o",
                   depends_on=[parent["ticket_id"]])
    wq._conn.execute("UPDATE work_queue SET status='failed' WHERE ticket_id=?",
                     (parent["ticket_id"],))
    wq._conn.commit()
    assert wq.get(child["ticket_id"])["status"] == "open"
    return parent["ticket_id"], child["ticket_id"]


def test_report_recascades_after_interrupted_cascade():
    """A real mid-report cascade interruption strands the child; the worker's
    retry (same token) re-drives the cascade instead of short-circuiting on
    'already_reported'."""
    wq, _ = _wq()
    parent = wq.add(privacy_class=PrivacyClass.public, payload="p", origin="o", max_attempts=1)
    child = wq.add(privacy_class=PrivacyClass.public, payload="c", origin="o",
                   depends_on=[parent["ticket_id"]])
    got = wq.claim_next("w", LOCAL, max=1)
    assert got and got[0]["ticket_id"] == parent["ticket_id"]
    token = got[0]["lease_token"]

    # Simulate the cascade raising AFTER the terminal transition committed.
    orig = wq._cascade_failure

    def boom(*a, **k):
        raise RuntimeError("store hiccup mid-cascade")

    wq._cascade_failure = boom
    try:
        wq.report("w", LOCAL, parent["ticket_id"], token,
                  WorkerResult(status="failed", summary="nope"))
    except RuntimeError:
        pass
    wq._cascade_failure = orig

    # Parent terminally failed, child stranded (un-cascaded).
    assert wq.get(parent["ticket_id"])["status"] == "failed"
    assert wq.get(child["ticket_id"])["status"] == "open"

    # The retry self-heals: cascade re-driven, child now terminal.
    out = wq.report("w", LOCAL, parent["ticket_id"], token,
                    WorkerResult(status="failed", summary="nope"))
    assert out == {"error": "already_reported"}
    assert wq.get(child["ticket_id"])["status"] == "failed"


def test_reject_recascades_after_interrupted_cascade():
    """An operator re-click of reject() on an already-failed parent re-drives a
    stranded cascade rather than only reporting 'not_in_review'."""
    wq, _ = _wq()
    parent_id, child_id = _parent_child_stranded(wq)

    out = wq.reject("reviewer", LOCAL, parent_id)

    assert out == {"error": "not_in_review"}
    assert wq.get(child_id)["status"] == "failed"


def test_reap_expired_reheals_stranded_cascade():
    """reap_expired's self-heal scan re-drives a cascade left interrupted by a
    prior tick or by report()/reject(): a terminally-failed parent is never
    re-selected by the lease-expiry UPDATEs, so without the scan its children
    stay blocked forever."""
    wq, _ = _wq()
    parent_id, child_id = _parent_child_stranded(wq)

    wq.reap_expired()

    assert wq.get(child_id)["status"] == "failed"


def test_reap_does_not_cascade_intentionally_parked_parent():
    """A secret_sensitive (intentionally parked, never-pullable) parent is NOT a
    failure: the reaper's cascade self-heal must exclude it (park_reason is not
    'max_attempts_exhausted'), or it would wrongly fail its dependents."""
    wq, _ = _wq()
    parent = wq.add(privacy_class=PrivacyClass.secret_sensitive, payload="s", origin="o")
    assert parent["park_reason"]  # parked, never open
    child = wq.add(privacy_class=PrivacyClass.secret_sensitive, payload="s2", origin="o",
                   depends_on=[parent["ticket_id"]])
    assert "ticket_id" in child  # both empty-tier -> monotonic edge allowed

    wq.reap_expired()

    # Neither is a failure; the intentionally-parked child must never be cascaded.
    assert wq.get(child["ticket_id"])["status"] != "failed"
