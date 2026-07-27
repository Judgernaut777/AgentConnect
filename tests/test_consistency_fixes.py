"""Ecosystem-review regression tests (docs/CONSISTENCY_REVIEW.md).

One test per confirmed finding: cancellation finality, lease/CAS fencing,
crash-recovery cascades, audit correlation, privacy enforcement, and cost
reconciliation must share consistent semantics across the local, rented, and
cloud dispatch paths. Everything runs offline and deterministically.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from agentconnect.common.authorization import AutoApproveSpendAuthorizer
from agentconnect.common.config import load_providers, load_routing
from agentconnect.common.memory import SharedMemory
from agentconnect.common.providers import ProviderRegistry
from agentconnect.common.schemas import (
    TaskConstraints,
    TaskState,
    TaskSubmission,
    Usage,
    WorkerResult as QueueWorkerResult,
)
from agentconnect.common.workqueue import WorkQueue
from agentconnect.core import (
    AgentConnectService,
    CogneeMemoryAdapter,
    Conflict,
    CreateTaskRequest,
    EchoWorker,
)
from agentconnect.core.models import (
    PrivacyTier,
    SandboxSpec,
    Subtask,
    SubtaskRequest,
    SubtaskStatus,
    WorkerLocation,
)
from agentconnect.core.models import WorkerRun as CoreWorkerRun  # noqa: F401
from agentconnect.core.routing import RoutePolicy
from agentconnect.core.workers import (
    WorkerAdapter,
    WorkerCapabilities,
    WorkerResult as CoreWorkerResult,
)
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.gateway import GatewayResult
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.provisioning import NodePool, StubProvisioner, spec_from_provider
from agentconnect.router.service import RouterService


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _router(memory=None, **kw):
    return RouterService.create(
        memory=memory or SharedMemory(),
        local_client=InProcessLocalClient(ResidencyManager()),
        **kw,
    )


def _submission(task="summarize the release notes", **constraints):
    return TaskSubmission(
        task=task, agent_type="log_summarizer",
        constraints=TaskConstraints(**constraints),
    )


def _task_state(mem, task_id):
    return mem.get_task(task_id)["state"]


# --------------------------------------------------------------------------- #
# 1. Router: a concurrent cancel wins over a slower completion (all 3 paths
#    share this single pipeline).
# --------------------------------------------------------------------------- #
def test_cancel_mid_flight_wins_over_completion():
    svc = _router()
    mem = svc.memory
    dispatch_started = threading.Event()
    release = threading.Event()

    def blocking_call(cfg, req):
        dispatch_started.set()
        assert release.wait(timeout=10)
        return GatewayResult(
            output_text="late result", input_tokens=5, output_tokens=5,
            provider=cfg.provider_id, model=req.model_id,
        )

    svc.gateway.call = blocking_call  # type: ignore[method-assign]

    out = {}

    def submit():
        out["summary"] = svc.submit_task(_submission())

    t = threading.Thread(target=submit, daemon=True)
    t.start()
    assert dispatch_started.wait(timeout=10)
    # The task is in-flight (RUNNING was written before dispatch).
    task_id = mem.list_tasks(limit=1)[0]["task_id"]
    assert _task_state(mem, task_id) == "RUNNING"

    cancel_res = svc.cancel_task(task_id)
    assert cancel_res.get("state") == "CANCELLED" and "error" not in cancel_res

    release.set()
    t.join(timeout=10)

    # Terminal CANCELLED was never clobbered by the late completion, and the
    # original submit caller sees the same answer as any later reader.
    assert _task_state(mem, task_id) == "CANCELLED"
    assert out["summary"].status == TaskState.CANCELLED
    assert mem.get_task(task_id)["summary"] == "Cancelled by manager."
    messages = [l["message"] for l in mem.get_log_slice(task_id, max_lines=100)]
    assert any("cancelled by manager" in m for m in messages)  # audit (finding 17)
    assert any("pipeline result discarded" in m for m in messages)


def test_cancel_task_already_terminal_is_noop_success():
    """Converged semantic (docs/CONSISTENCY_REVIEW.md adjudication, goal item
    1's explicit directive): cancel of an already-terminal task is a NO-OP
    SUCCESS reporting the stored state, on both engines — never error-shaped.
    This test used to pin the opposite ("core raises Conflict; router returns
    a typed error"), a divergence the goal explicitly asks to converge away in
    favor of the one shape already proven safe under concurrent/retried
    callers (`WorkQueue.cancel_for_task`'s). Terminal-finality — the actual
    safety property this test protects — is asserted below just as strictly:
    the state is still reported as COMPLETE, never silently changed.
    """
    svc = _router()
    summary = svc.submit_task(_submission())
    assert summary.status == TaskState.COMPLETE
    res = svc.cancel_task(summary.task_id)
    assert "error" not in res
    assert res["already_terminal"] is True
    assert res["state"] == "COMPLETE"


# --------------------------------------------------------------------------- #
# 2. Core: a cancelled subtask cannot be resurrected by a stale in-flight
#    worker.run() (zombie write).
# --------------------------------------------------------------------------- #
class _SlowEchoWorker(EchoWorker):
    def __init__(self, gate):
        super().__init__("slow_worker")
        self._gate = gate

    def run(self, subtask, context):
        assert self._gate.wait(timeout=10)
        return super().run(subtask, context)

    def cancel(self, run_id):  # advisory no-op: cannot unwind in-flight Python
        return None


def test_cancel_subtask_not_resurrected_by_slow_worker(tmp_path):
    gate = threading.Event()
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"),
        workers=[_SlowEchoWorker(gate)],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))

    def submit():
        try:
            svc.submit_subtask(task.id, SubtaskRequest(title="s", instructions="do it"))
        except Exception:
            pass

    t = threading.Thread(target=submit, daemon=True)
    t.start()
    deadline = time.time() + 10
    subtask_id = None
    while time.time() < deadline:
        subs = svc.storage.list_subtasks(task.id)
        if subs and subs[0].status is SubtaskStatus.running:
            subtask_id = subs[0].id
            break
        time.sleep(0.01)
    assert subtask_id is not None, "subtask never reached running"

    svc.cancel_subtask(subtask_id)
    assert svc.storage.get_subtask(subtask_id).status is SubtaskStatus.cancelled

    gate.set()
    t.join(timeout=10)

    final = svc.storage.get_subtask(subtask_id)
    assert final.status is SubtaskStatus.cancelled  # terminal is final
    assert final.result_artifact_id is None  # no zombie artifact attachment
    assert all(r.status.value != "succeeded" for r in svc.storage.list_runs(subtask_id))
    # The stale result is auditable, not silent.
    detail = svc.get_task(task.id)
    assert any("stale worker result" in a.summary for a in detail.attempts)


# --------------------------------------------------------------------------- #
# 3/7. WorkQueue: a valid, correctly-fenced report()/reap can no longer
#      resurrect a CANCELLED task; the refusal is audited.
# --------------------------------------------------------------------------- #
def _queue(mem=None):
    mem = mem or SharedMemory()
    return mem, WorkQueue(mem, load_routing())


def _force_state(mem, task_id, state):
    """Test-only state seeding through the ONE legitimate `tasks.state` writer
    (`transition_task`, the authority's LockedWriter). The old shortcut — a
    bare `update_task` with a `state` keyword — is now rejected at runtime, exactly
    so no production caller can flip the column outside the authority; tests
    that need to TELEPORT a task into a scenario state (including deliberately
    illegal jumps that simulate crash windows) hand-roll the decide closure."""
    applied, stored = mem.transition_task(task_id, lambda _cur: ({"state": state}, None))
    assert applied and stored == state


def test_workqueue_report_cannot_resurrect_cancelled_task():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    _force_state(mem, task_id, "RUNNING")
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    # Another engine cancels the task while the lease is still valid.
    _force_state(mem, task_id, "CANCELLED")

    out = wq.report("worker-1", "local_only", ticket["ticket_id"],
                    got["lease_token"], {"status": "completed"})
    assert "error" not in out  # the ticket-side report is still recorded
    assert _task_state(mem, task_id) == "CANCELLED"  # ...but terminal is final
    messages = [l["message"] for l in mem.get_log_slice(task_id, max_lines=50)]
    assert any("refused task-state overwrite" in m for m in messages)  # audited


def test_workqueue_reap_cannot_resurrect_cancelled_task():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    _force_state(mem, task_id, "RUNNING")
    wq.add(task="x", origin="t", privacy_class="public", payload="p",
           task_id=task_id, max_attempts=1)
    wq.claim_next("worker-1", "local_only", capabilities=[], lease_seconds=1)
    _force_state(mem, task_id, "CANCELLED")
    wq.reap_expired(now=time.time() + 3600)  # attempts exhausted -> park path
    assert _task_state(mem, task_id) == "CANCELLED"


def test_workqueue_task_state_transitions_are_audited():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    _force_state(mem, task_id, "RUNNING")
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    assert _task_state(mem, task_id) == "COMPLETE"
    messages = [l["message"] for l in mem.get_log_slice(task_id, max_lines=50)]
    assert any("work_queue task-state RUNNING -> COMPLETE" in m for m in messages)


# --------------------------------------------------------------------------- #
# 4. Cancellation is honored at claim time, and cancel_task cancels linked
#    tickets.
# --------------------------------------------------------------------------- #
def test_claim_refuses_ticket_of_cancelled_task_and_cancel_reaches_queue():
    svc = _router()
    ticket = svc.enqueue_task(_submission("queue this work"))
    task_id = svc.workqueue.get(ticket["ticket_id"])["task_id"]

    res = svc.cancel_task(task_id)
    assert res["state"] == "CANCELLED"
    assert ticket["ticket_id"] in res["cancelled_tickets"]
    # The ticket is terminally cancelled...
    assert svc.workqueue.get(ticket["ticket_id"])["status"] == "cancelled"
    # ...and nothing is claimable.
    assert svc.workqueue.claim_next("worker-1", "local_only",
                                    capabilities=["summarization"], max=10) == []


def test_claim_gate_is_atomic_against_cancelled_task_even_if_ticket_open():
    # Even when the ticket row itself is still 'open' (e.g. the task was
    # cancelled by an engine that doesn't know about the queue), the guarded
    # claim UPDATE refuses it.
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    _force_state(mem, task_id, "CANCELLED")
    assert wq.claim_next("worker-1", "local_only", capabilities=[]) == []


def test_report_on_cancelled_ticket_is_typed_error():
    svc = _router()
    ticket = svc.enqueue_task(_submission("queue this work"))
    got = svc.workqueue.claim_next(
        "worker-1", "local_only", capabilities=["summarization"])[0]
    task_id = svc.workqueue.get(ticket["ticket_id"])["task_id"]
    svc.cancel_task(task_id)
    out = svc.workqueue.report("worker-1", "local_only", ticket["ticket_id"],
                               got["lease_token"], {"status": "completed"})
    assert out == {"error": "ticket_cancelled"}
    assert _task_state(svc.memory, task_id) == "CANCELLED"


# --------------------------------------------------------------------------- #
# 6. run_subtask: queued -> running is a fenced compare-and-set; no
#    double-execution.
# --------------------------------------------------------------------------- #
class _CountingWorker(WorkerAdapter):
    def __init__(self, barrier):
        self.run_count = 0
        self._barrier = barrier

    @property
    def worker_id(self):
        return "counting_worker"

    def capabilities(self):
        # Rendezvous BEFORE the claim: both racing threads reach here, then
        # both attempt the guarded queued->running UPDATE.
        try:
            self._barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return WorkerCapabilities(
            worker_id="counting_worker", harness="test", model=None, tools=[],
            sandbox=SandboxSpec(), privacy_tiers=list(PrivacyTier),
            capability_tags=["echo"], location=WorkerLocation.local,
        )

    def run(self, subtask, context):
        self.run_count += 1
        return CoreWorkerResult(status="succeeded", summary="ran once")


def test_run_subtask_concurrent_callers_execute_worker_exactly_once(tmp_path):
    barrier = threading.Barrier(2)
    worker = _CountingWorker(barrier)
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[worker],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    now = time.time()
    sub = Subtask(
        id="sub_race_1", parent_task_id=task.id, title="s", instructions="i",
        status=SubtaskStatus.queued, privacy_tier=PrivacyTier.repo_sensitive,
        created_at=now, updated_at=now,
        route_reason={"subtask_id": "sub_race_1", "selected_worker": "counting_worker"},
    )
    svc.storage.insert_subtask(sub)

    results, errors = [], []

    def runner():
        try:
            results.append(svc.run_subtask("sub_race_1"))
        except Conflict as exc:
            errors.append(exc)

    threads = [threading.Thread(target=runner, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert worker.run_count == 1  # the whole point
    # The loser surfaced a typed Conflict (or arrived late enough to see the
    # terminal state) — never a silent second execution.
    assert len(results) + len(errors) == 2
    assert svc.storage.get_subtask("sub_race_1").status is SubtaskStatus.succeeded


# --------------------------------------------------------------------------- #
# 8. NodePool: concurrent acquires provision exactly once (no orphaned box).
# --------------------------------------------------------------------------- #
def test_nodepool_concurrent_acquire_provisions_once():
    class SlowProv(StubProvisioner):
        def provision(self, spec):
            time.sleep(0.05)  # widen the check-then-provision span
            return super().provision(spec)

    prov = SlowProv()
    reg = ProviderRegistry.from_config(load_providers())
    cfg = reg.get("rented_h100_pool")
    spec = spec_from_provider(cfg, model_id="m")
    pool = NodePool()
    out = []

    def acquire():
        out.append(pool.acquire(cfg, prov, spec, now=1.0))

    threads = [threading.Thread(target=acquire, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert prov._counter == 1  # exactly one provision
    node_ids = {h.node_id for h, _ in out}
    assert len(node_ids) == 1  # both callers share the one node
    assert sorted(reused for _, reused in out) == [False, True]
    assert len(pool.live_nodes()) == 1  # nothing orphaned/untracked


# --------------------------------------------------------------------------- #
# 9. reconcile_orphans cascades dependency failure (no stranded blocked
#    sibling).
# --------------------------------------------------------------------------- #
def test_reconcile_orphans_cascades_to_blocked_siblings(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    now = time.time()
    # Subtask A: crashed mid-run (live 'running' run row, no terminal event).
    sub_a = Subtask(
        id="sub_orphan_a", parent_task_id=task.id, title="a", instructions="i",
        status=SubtaskStatus.running, privacy_tier=PrivacyTier.repo_sensitive,
        created_at=now, updated_at=now,
    )
    svc.storage.insert_subtask(sub_a)
    from agentconnect.core.models import RunStatus, WorkerRun
    from agentconnect.core import ids as _ids

    svc.storage.insert_run(WorkerRun(
        id=_ids.new_id(_ids.RUN), subtask_id="sub_orphan_a", worker_id="echo_worker",
        harness="echo", model=None, status=RunStatus.running, started_at=now - 100,
    ))
    # Subtask B: blocked on A.
    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="b", instructions="i", depends_on=["sub_orphan_a"],
    ))
    assert b.status is SubtaskStatus.blocked

    report = svc.reconcile_orphans(older_than_seconds=0)
    assert report["reconciled_runs"], "orphaned run was not swept"
    assert svc.storage.get_subtask("sub_orphan_a").status is SubtaskStatus.failed
    # The previously-stranded sibling is cascade-failed, exactly like the
    # normal _record_result terminal-failure path.
    assert svc.storage.get_subtask(b.id).status is SubtaskStatus.failed


# --------------------------------------------------------------------------- #
# 10. A crashed rented node is evicted, never reused forever.
# --------------------------------------------------------------------------- #
def _rented_router(client_factory):
    svc = RouterService.create(
        memory=SharedMemory(), local_client=None,
        provisioner=StubProvisioner(), rented_client_factory=client_factory,
        authorizer=AutoApproveSpendAuthorizer(),
    )
    svc.set_budget(50.0, "monthly")
    return svc


def _rented_submission():
    return TaskSubmission(
        task="big private job", agent_type="repo_scout",
        constraints=TaskConstraints(privacy_class="repo_sensitive",
                                    allow_external=False, allow_rented=True),
    )


def test_crashed_rented_node_is_evicted_not_reused():
    class BoomClient:
        def generate(self, req):
            raise ConnectionError("inference backend crashed")

    svc = _rented_router(lambda cfg, handle: BoomClient())
    summary = svc.submit_task(_rented_submission())
    assert summary.status == TaskState.FAILED  # per-task failure stays loud
    # The poisoned handle is GONE from the pool: the next task re-provisions
    # instead of silently reusing a dead node forever.
    assert svc.node_pool.live_nodes() == {}
    messages = [l["message"] for l in svc.memory.get_log_slice(summary.task_id, max_lines=50)]
    assert any("rented_node_evicted" in m for m in messages)


# --------------------------------------------------------------------------- #
# 14. Rented cost: spin-up billed even on failure; idle reap trues up the
#     overrun beyond the billed window.
# --------------------------------------------------------------------------- #
def test_reap_idle_trues_up_rental_cost_beyond_billed_window():
    factory = lambda cfg, handle: InProcessLocalClient(ResidencyManager())
    svc = _rented_router(factory)
    assert svc.submit_task(_rented_submission()).status == TaskState.COMPLETE
    cfg = svc.registry.get("rented_h100_pool")
    reaped = svc.reap_idle_nodes(now=time.time() + 10_000)
    assert "rented_h100_pool" in reaped
    rows = svc.memory._conn.execute(
        "SELECT status, act_cost_usd FROM quota_records WHERE provider=?",
        ("rented_h100_pool",),
    ).fetchall()
    statuses = {r["status"] for r in rows}
    assert "rented" in statuses  # spin-up window billed once
    true_ups = [r for r in rows if r["status"] == "rental_true_up"]
    assert true_ups and true_ups[0]["act_cost_usd"] > 0  # overrun reconciled


# --------------------------------------------------------------------------- #
# 11. MCP-only deployments get the same lease-expiry self-healing as HTTP.
# --------------------------------------------------------------------------- #
def test_mcp_server_starts_workqueue_reaper():
    pytest.importorskip("mcp")
    from agentconnect.router.mcp_server import build_mcp_server

    svc = _router()
    server = build_mcp_server(service=svc, worker_tiers={}, reaper_interval=0.05)
    thread, stop = server._agentconnect_reaper
    try:
        assert thread.is_alive()
        ticket = svc.workqueue.add(task="x", origin="t", privacy_class="public",
                                   payload="p")
        svc.workqueue.claim_next("worker-1", "local_only", capabilities=[],
                                 lease_seconds=0)
        deadline = time.time() + 5
        while time.time() < deadline:
            if svc.workqueue.get(ticket["ticket_id"])["status"] == "open":
                break
            time.sleep(0.02)
        # The dead worker's expired lease was requeued with no manual call.
        assert svc.workqueue.get(ticket["ticket_id"])["status"] == "open"
    finally:
        stop.set()
        thread.join(timeout=2)


def test_mcp_server_reaper_can_be_disabled():
    pytest.importorskip("mcp")
    from agentconnect.router.mcp_server import build_mcp_server

    server = build_mcp_server(service=_router(), worker_tiers={}, reaper_interval=0)
    assert not hasattr(server, "_agentconnect_reaper")


# --------------------------------------------------------------------------- #
# 12. secret_sensitive task content never reaches a memory backend.
# --------------------------------------------------------------------------- #
def test_secret_sensitive_task_content_never_sent_to_memory_backend(tmp_path):
    calls = []

    def spy(method, url, payload):
        calls.append((method, url, payload))
        return {"results": []}

    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()],
        memory_backends={"cognee": CogneeMemoryAdapter(transport=spy)},
    )
    secret_task = svc.create_task(CreateTaskRequest(
        title="rotate prod creds",
        goal="the leaked key is AKIAABCDEFGHIJKLMNOP, rotate it",
        metadata={"privacy": "secret_sensitive"},
    ))
    assert svc.get_task(secret_task.id).effective_privacy is PrivacyTier.secret_sensitive
    pack = svc.get_task_context_pack(secret_task.id, profile="broad_project_rag")
    assert calls == []  # nothing left the box
    assert pack.backends_queried == []
    assert any("withheld" in w for w in pack.warnings)  # never silent

    # Control: a non-secret task still recalls normally.
    public_task = svc.create_task(CreateTaskRequest(title="write docs", goal="explain the API"))
    svc.get_task_context_pack(public_task.id, profile="broad_project_rag")
    assert len(calls) == 1
    assert "AKIA" not in json.dumps(calls)


# --------------------------------------------------------------------------- #
# 13. enqueue_task dedup retries leak no orphan ledger rows.
# --------------------------------------------------------------------------- #
def test_enqueue_dedup_retry_leaks_no_orphan_rows():
    svc = _router()
    mem = svc.memory
    tickets = [svc.enqueue_task(_submission("retry me"), dedup_key="retry-job-1")
               for _ in range(3)]
    assert len({t["ticket_id"] for t in tickets}) == 1  # dedup still works
    n_tasks = mem._conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    n_payloads = mem._conn.execute(
        "SELECT COUNT(*) AS n FROM artifacts WHERE kind='sanitized_payload'"
    ).fetchone()["n"]
    assert n_tasks == 1  # was 3 before the fix
    assert n_payloads == 1


# --------------------------------------------------------------------------- #
# 15. (end-to-end) a live cloud failure is a FAILED task with failure-shaped
#     quota reconciliation — never a fabricated COMPLETE + cost.
# --------------------------------------------------------------------------- #
def test_cloud_outage_end_to_end_is_failed_not_fabricated_complete():
    from agentconnect.router.gateway import ProviderGateway

    class _Secrets:
        def resolve(self, ref):
            return "sk-live"

    def boom(**kwargs):
        raise RuntimeError("provider 500")

    gw = ProviderGateway(secret_resolver=_Secrets(), completion_fn=boom)
    svc = RouterService.create(
        memory=SharedMemory(), gateway=gw,
        authorizer=AutoApproveSpendAuthorizer(),
    )
    svc.set_budget(50.0, "monthly")
    summary = svc.submit_task(TaskSubmission(
        task="review this public patch", agent_type="patch_reviewer",
        constraints=TaskConstraints(allow_paid=True),
    ))
    assert summary.status == TaskState.FAILED
    usage = svc.memory.quota_usage_since("openai_paid", 0)
    assert usage["requests"] == 0  # no fabricated completed request
    assert usage["cost"] == 0  # no fabricated cost


# --------------------------------------------------------------------------- #
# 16. Engine A: cumulative spend cap over recorded run spend.
# --------------------------------------------------------------------------- #
class _PaidWorker(WorkerAdapter):
    def __init__(self):
        self.calls = 0

    @property
    def worker_id(self):
        return "paid_worker"

    def capabilities(self):
        return WorkerCapabilities(
            worker_id="paid_worker", harness="raw", model="m", tools=[],
            sandbox=SandboxSpec(), privacy_tiers=list(PrivacyTier),
            capability_tags=["echo"], location=WorkerLocation.local,
            cost_per_1k_tokens_usd=0.5, requires_approval=False,
        )

    def run(self, subtask, context):
        self.calls += 1
        return CoreWorkerResult(
            status="succeeded", summary="ran",
            metrics={"estimated_cost_usd": 0.4},
        )


def test_cumulative_spend_cap_stops_unbounded_per_call_spend(tmp_path):
    worker = _PaidWorker()
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[worker],
        policy=RoutePolicy(max_cost_usd=10.0, max_total_cost_usd=1.0),
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    instructions = "x" * 2000  # per-call estimate ~$0.31: under the per-call cap

    first = svc.submit_subtask(task.id, SubtaskRequest(title="s1", instructions=instructions))
    second = svc.submit_subtask(task.id, SubtaskRequest(title="s2", instructions=instructions))
    assert first.status is SubtaskStatus.succeeded
    assert second.status is SubtaskStatus.succeeded
    assert worker.calls == 2
    assert svc.storage.total_run_cost_usd() == pytest.approx(0.8)

    # Third call: each call is individually under the per-call ceiling, but the
    # CUMULATIVE recorded spend + estimate now exceeds the total cap.
    third = svc.submit_subtask(task.id, SubtaskRequest(title="s3", instructions=instructions))
    assert third.status is SubtaskStatus.failed
    assert worker.calls == 2  # never executed
    reasons = [r["reason"] for r in (third.route_reason or {}).get("rejected_workers", [])]
    assert any("cumulative spend" in r for r in reasons)


# --------------------------------------------------------------------------- #
# 17. WorkQueue: worker-reported usage/cost lands on the evaluation row.
# --------------------------------------------------------------------------- #
def test_workqueue_report_records_tokens_and_cost_on_evaluation():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    result = QueueWorkerResult(
        status="completed", summary="done", confidence=0.9,
        usage=Usage(input_tokens=5000, output_tokens=2000, model_id="m",
                    cost_usd=0.12),
    )
    out = wq.report("worker-1", "local_only", ticket["ticket_id"],
                    got["lease_token"], result)
    assert "error" not in out
    row = mem._conn.execute(
        "SELECT input_tokens, output_tokens, cost_usd, model FROM evaluations"
        " WHERE task_id=?", (task_id,),
    ).fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (5000, 2000)
    assert row["cost_usd"] == pytest.approx(0.12)
    assert row["model"] == "m"


# --------------------------------------------------------------------------- #
# Low advisory: the task-state mirror self-heals after a crash between the
# terminal ticket commit and _mirror_task_state.
# --------------------------------------------------------------------------- #
def test_task_state_mirror_self_heals_on_report_retry_and_reap():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    _force_state(mem, task_id, "RUNNING")
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    assert _task_state(mem, task_id) == "COMPLETE"
    # Simulate the crash window: the ticket-terminal commit landed but the task
    # mirror write was lost.
    _force_state(mem, task_id, "RUNNING")

    # A retried report (idempotent 'already_reported') re-drives the mirror.
    out = wq.report("worker-1", "local_only", ticket["ticket_id"],
                    got["lease_token"], {"status": "completed"})
    assert out == {"error": "already_reported"}
    assert _task_state(mem, task_id) == "COMPLETE"

    # And so does the periodic reaper, with no report retry at all.
    _force_state(mem, task_id, "RUNNING")
    wq.reap_expired(now=time.time())
    assert _task_state(mem, task_id) == "COMPLETE"
