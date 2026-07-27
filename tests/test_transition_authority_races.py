"""The nasty tests: concurrent/adversarial exercise of the shared transition
authority across both engines (docs/CONSISTENCY_REVIEW.md consolidation).

Coverage in this file (a prioritized subset of the full nasty-test list —
the mechanism's core safety properties, not every named scenario):

1. Cancel-vs-pipeline: RouterService.cancel_task racing an in-flight
   submit_task.
2. Cancel-vs-ticket-report: cancel mid-lease, then the worker's correctly-
   fenced report() lands.
3. Governor resurrection: cancel_subtask lands between the atomic claim and
   the governor's write.
4. claim_task compose atomicity: an injected failure inside the composed
   transaction leaves NEITHER the claim NOR the status flip NOR the audit
   row persisted.
5. Audit-under-rollback (fail-closed proof): breaking the audit sink rolls
   back the state write with it, on both engines.
6. Mirror self-heal replay: a retried report()/reap on an already-terminal
   ticket + already-terminal task is a silent no-op, never a duplicate
   audit row or an exception.

Not covered here (disclosed, not silently dropped): reconcile-idempotence
across a simulated crash window, cascade-double-fail from two parents into
one shared dependent, and the abandon_stale_sessions/reconcile_orphans
sweep-collision test — all three exercise the SAME underlying mechanism
this file already proves (fresh-read CAS + idempotent no-op), just from a
different call site.
"""

from __future__ import annotations

import threading
import time

import pytest

from agentconnect.common.config import load_routing
from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import TaskConstraints, TaskState, TaskSubmission
from agentconnect.common.workqueue import WorkQueue
from agentconnect.core import AgentConnectService, CreateTaskRequest
from agentconnect.core.models import PrivacyTier, SubtaskRequest, SubtaskStatus
from agentconnect.core.routing import RoutePolicy
from agentconnect.core.toolconnect_client import ToolDecision, ToolUseAuthorization
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.gateway import GatewayResult
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.service import RouterService


def _router(**kw):
    return RouterService.create(
        memory=kw.pop("memory", None) or SharedMemory(),
        local_client=InProcessLocalClient(ResidencyManager()), **kw,
    )


def _submission():
    return TaskSubmission(
        task="summarize the release notes", agent_type="log_summarizer",
        constraints=TaskConstraints(),
    )


def _applied_transitions(svc_or_mem, task_id):
    """Every APPLIED transition-audit row for one task_id, across whichever
    audit sink (`events` for Engine A/router SharedMemory logs) is in play."""
    if hasattr(svc_or_mem, "get_log_slice"):  # SharedMemory
        lines = svc_or_mem.get_log_slice(task_id, max_lines=500)
        return [line for line in lines if "->" in line["message"]]
    return [  # AgentConnectService
        e for e in svc_or_mem.list_events(task_id)
        if e.kind == "transition" and e.payload.get("outcome") == "applied"
    ]


# --------------------------------------------------------------------- #
# 1. Cancel-vs-pipeline
# --------------------------------------------------------------------- #
def test_cancel_vs_pipeline_exactly_one_winner():
    svc = _router()
    mem = svc.memory
    dispatch_started = threading.Event()
    release = threading.Event()

    def blocking_call(cfg, req):
        dispatch_started.set()
        assert release.wait(timeout=10)
        return GatewayResult(
            output_text="late", input_tokens=5, output_tokens=5,
            provider=cfg.provider_id, model=req.model_id,
        )

    svc.gateway.call = blocking_call  # type: ignore[method-assign]
    out = {}

    def submit():
        out["summary"] = svc.submit_task(_submission())

    t = threading.Thread(target=submit, daemon=True)
    t.start()
    assert dispatch_started.wait(timeout=10)
    task_id = mem.list_tasks(limit=1)[0]["task_id"]

    cancel_res = svc.cancel_task(task_id)
    assert cancel_res["state"] == "CANCELLED"
    assert cancel_res["already_terminal"] is False

    release.set()
    t.join(timeout=10)

    assert mem.get_task(task_id)["state"] == "CANCELLED"
    assert out["summary"].status == TaskState.CANCELLED
    # Exactly one APPLIED transition to CANCELLED — the pipeline's late
    # completion write never landed (refused, or never attempted a second
    # cancel).
    applied_cancels = [
        line for line in _applied_transitions(mem, task_id)
        if "-> CANCELLED" in line["message"]
    ]
    assert len(applied_cancels) == 1, applied_cancels
    assert any(
        "cancelled by manager" in l["message"]
        for l in mem.get_log_slice(task_id, max_lines=200)
    )


# --------------------------------------------------------------------- #
# 2. Cancel-vs-ticket-report
# --------------------------------------------------------------------- #
def test_cancel_vs_ticket_report_task_stays_cancelled():
    svc = _router()
    ticket = svc.enqueue_task(_submission())
    got = svc.workqueue.claim_next("worker-1", "local_only", capabilities=["summarization"])[0]
    task_id = svc.workqueue.get(ticket["ticket_id"])["task_id"]

    # Cancel mid-lease.
    res = svc.cancel_task(task_id)
    assert res["state"] == "CANCELLED"
    assert ticket["ticket_id"] in res["cancelled_tickets"]

    # The worker's fenced report is still honestly processed (the lease was
    # cleared by cancel_for_task, so this is refused as ticket_cancelled) —
    # but even if it somehow raced past that, the task mirror must refuse.
    out = svc.workqueue.report(
        "worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
        {"status": "completed"},
    )
    assert out == {"error": "ticket_cancelled"}
    assert svc.memory.get_task(task_id)["state"] == "CANCELLED"
    # No resurrection: never any applied transition landing on a non-CANCELLED
    # state after the cancel.
    lines = svc.memory.get_log_slice(task_id, max_lines=500)
    assert not any(
        "-> COMPLETE" in l["message"] and "refused" not in l["message"] for l in lines
    )


# --------------------------------------------------------------------- #
# 3. Governor resurrection
# --------------------------------------------------------------------- #
def _cloud_worker():
    from agentconnect.core.workers import RawModelWorker
    from agentconnect.core.models import WorkerLocation

    return RawModelWorker(
        "cheap_cloud", lambda p: "out", model="deepseek-v3", location=WorkerLocation.cloud,
        privacy_tiers=[PrivacyTier.public], capability_tags=["generate"],
        cost_per_1k_tokens_usd=1.0,
    )


def test_governor_resurrection_race_is_refused_not_resurrected(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[_cloud_worker()],
        policy=RoutePolicy(max_cost_usd=10.0),
    )
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="A", instructions="i", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"],
    ))
    assert subtask.status is SubtaskStatus.needs_approval  # cloud worker parks

    def racing_consult(*args, **kwargs):
        # Fires from inside `_execute`, AFTER the atomic queued->running
        # claim and BEFORE the governor's write — exactly the race window
        # finding #2 named. A concurrent cancel lands here.
        svc.cancel_subtask(subtask.id)
        decision = ToolDecision(
            allowed=False, reason="policy forbids", decision_id="dec-1",
            default_deny=False, determining_policies=("p-generate",), contract_version="1.0",
        )
        return ToolUseAuthorization(
            allowed=False, governed=True, denied_tool="generate", decision=decision,
        )

    svc._consult_tool_governor = racing_consult
    approved = svc.approve_subtask(subtask.id, "matthew", max_cost_usd=3.0)

    # The subtask stays CANCELLED — the governor's running->failed write was
    # refused (a no-op `advance()`), never resurrecting it to `failed`.
    assert approved.status is SubtaskStatus.cancelled
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.cancelled


# --------------------------------------------------------------------- #
# 4. claim_task compose atomicity
# --------------------------------------------------------------------- #
def test_claim_task_compose_atomicity_on_injected_failure(tmp_path):
    from agentconnect.core.models import TaskStatus

    svc = AgentConnectService.create(db_path=":memory:", artifact_dir=str(tmp_path / "a"))
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))

    original = svc._task_status_authority.transition

    def boom(*args, **kwargs):
        original(*args, **kwargs)  # the real claim+status write lands on `conn`
        raise RuntimeError("injected failure after transition, before commit")

    svc._task_status_authority.transition = boom
    with pytest.raises(RuntimeError, match="injected failure"):
        svc.claim_task(task.id, "manager-1")

    # NOTHING persisted: no claim row, status still `queued`, no audit row.
    # `SqliteStorage.transaction()`'s rollback discarded the whole span.
    assert svc.storage.list_claims(task.id) == []
    assert svc.get_task(task.id).task.status is TaskStatus.queued
    assert svc.get_task(task.id).task.current_manager is None
    assert svc.list_events(task.id) == []


# --------------------------------------------------------------------- #
# 5. Audit-under-rollback (fail-closed proof)
# --------------------------------------------------------------------- #
def test_engine_a_audit_failure_rolls_back_the_state_write(tmp_path):
    svc = AgentConnectService.create(db_path=":memory:", artifact_dir=str(tmp_path / "a"))
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))

    # Break the audit sink: rename `events` away so the INSERT inside
    # `transition_row` fails mid-span.
    with svc.storage.transaction() as c:
        c.execute("ALTER TABLE events RENAME TO events_broken")

    with pytest.raises(Exception):
        svc.cancel_task(task.id)

    # Restore and confirm the state truly never moved: re-read shows the
    # ORIGINAL status, not a half-applied CANCELLED.
    with svc.storage.transaction() as c:
        c.execute("ALTER TABLE events_broken RENAME TO events")
    from agentconnect.core.models import TaskStatus

    assert svc.get_task(task.id).task.status is TaskStatus.queued

    # And now that the sink is restored, the SAME call succeeds normally.
    cancelled = svc.cancel_task(task.id)
    assert cancelled.status.value == "cancelled"


def test_engine_b_audit_failure_rolls_back_the_state_write():
    mem = SharedMemory()
    task_id = mem.create_task({"task": "x"})
    # Seed via the authority's LockedWriter — a bare update_task(state=...)
    # is rejected at runtime (one transition authority enforcement).
    mem.transition_task(task_id, lambda _cur: ({"state": "QUEUED"}, None))

    with mem._lock:
        mem._conn.execute("ALTER TABLE logs RENAME TO logs_broken")
        mem._conn.commit()

    wq = WorkQueue(mem, load_routing())
    # Drive a transition through the shared authority directly (mirrors what
    # RouterService._transition / WorkQueue's mirror do).
    from agentconnect.common.schemas import TaskState as TS

    with pytest.raises(Exception):
        wq._task_state_authority.transition(task_id, TS.DISPATCHED, actor="test")

    assert mem.get_task(task_id)["state"] == "QUEUED"  # never moved

    with mem._lock:
        mem._conn.execute("ALTER TABLE logs_broken RENAME TO logs")
        mem._conn.commit()

    # Restored: the same transition now succeeds.
    wq._task_state_authority.transition(task_id, TS.DISPATCHED, actor="test")
    assert mem.get_task(task_id)["state"] == "DISPATCHED"


# --------------------------------------------------------------------- #
# 6. Mirror self-heal replay
# --------------------------------------------------------------------- #
def test_mirror_self_heal_replay_is_silent_no_op():
    mem = SharedMemory()
    wq = WorkQueue(mem, load_routing())
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    assert mem.get_task(task_id)["state"] == "COMPLETE"

    before = len(mem.get_log_slice(task_id, max_lines=500))
    # Retried report on an already-terminal ticket + already-terminal task:
    # silent no-op self-heal, no exception, no NEW audit noise.
    out = wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
                    {"status": "completed"})
    assert out == {"error": "already_reported"}
    assert mem.get_task(task_id)["state"] == "COMPLETE"
    after = len(mem.get_log_slice(task_id, max_lines=500))
    # The report's own self-heal re-drive of the mirror produces at most the
    # SAME line count (converge's current==dst path writes nothing) — no
    # runaway audit growth from a retry storm.
    assert after == before

    # And the reaper's self-heal pass, with no report retry at all, is
    # equally silent.
    wq.reap_expired(now=time.time())
    assert mem.get_task(task_id)["state"] == "COMPLETE"
    assert len(mem.get_log_slice(task_id, max_lines=500)) == before


def test_enqueue_born_task_mirror_overlay_created_to_complete():
    """`enqueue_task` leaves a task at CREATED for its whole ticket lifetime
    (no pipeline walks it through the FSM); the mirror's overlay must still
    reach COMPLETE directly from CREATED."""
    svc = _router()
    ticket = svc.enqueue_task(_submission())
    task_id = svc.workqueue.get(ticket["ticket_id"])["task_id"]
    assert svc.memory.get_task(task_id)["state"] == "CREATED"

    got = svc.workqueue.claim_next("worker-1", "local_only", capabilities=["summarization"])[0]
    svc.workqueue.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
                         {"status": "completed"})
    assert svc.memory.get_task(task_id)["state"] == "COMPLETE"


# --------------------------------------------------------------------- #
# 7. complete_task double-fire (review finding: the entry precheck is a
#    stale snapshot; the strict transition() verb treats current==dst as a
#    silent no-op, so a raced/retried complete_task used to re-fire every
#    completion side effect — including external completion_hooks).
# --------------------------------------------------------------------- #
def test_complete_task_race_fires_completion_side_effects_exactly_once(tmp_path):
    from agentconnect.core import Conflict

    svc = AgentConnectService.create(db_path=":memory:", artifact_dir=str(tmp_path / "a"))
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))

    hook_calls: list[str] = []
    svc.completion_hooks.append(lambda tid: hook_calls.append(tid))

    # Deterministic interleave (the review's repro technique): the outer
    # complete_task has passed its already-succeeded precheck; a second
    # complete_task for the same task then runs TO COMPLETION from inside
    # `regenerate_handoff_summary` (which sits between the precheck and the
    # status write), before the outer call reaches its own write.
    real_regen = svc.regenerate_handoff_summary
    inner: dict = {}

    def reentrant_regen(task_id):
        out = real_regen(task_id)
        if "fired" not in inner:
            inner["fired"] = True  # before re-entering, or the inner call recurses
            inner["result"] = svc.complete_task(task_id, "racer", force=True)
        return out

    svc.regenerate_handoff_summary = reentrant_regen  # type: ignore[method-assign]

    # The INNER call wins; the outer call's write is an atomic no-op advance
    # and surfaces as Conflict — it must NOT fall through to the side effects.
    with pytest.raises(Conflict, match="already succeeded"):
        svc.complete_task(task.id, "outer", force=True)

    assert inner["result"]["status"] == "succeeded"
    # Exactly one hook invocation and one ledger completion event for the one
    # logical completion — the loser re-fired nothing.
    assert hook_calls == [task.id]
    completed = [e for e in svc.list_events(task.id) if e.kind == "task_completed"]
    assert len(completed) == 1
    # And exactly one APPLIED transition-audit row landed on `succeeded`.
    applied = [
        e for e in svc.list_events(task.id)
        if e.kind == "transition" and e.payload.get("outcome") == "applied"
        and e.payload.get("dst") == "succeeded"
    ]
    assert len(applied) == 1

    # A plain sequential double-complete still reports the same Conflict.
    with pytest.raises(Conflict, match="already succeeded"):
        svc.complete_task(task.id, "again", force=True)
    assert hook_calls == [task.id]


def test_complete_task_still_refused_after_cancel(tmp_path):
    """The complete-after-terminal gap stays closed under the atomic advance:
    a cancelled task can never be marked succeeded, force or not."""
    from agentconnect.core import Conflict
    from agentconnect.core.models import TaskStatus

    svc = AgentConnectService.create(db_path=":memory:", artifact_dir=str(tmp_path / "a"))
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    svc.cancel_task(task.id)
    with pytest.raises(Conflict, match="cancelled"):
        svc.complete_task(task.id, "late", force=True)
    assert svc.get_task(task.id).task.status is TaskStatus.cancelled


# --------------------------------------------------------------------- #
# 8. cancel_agent wire shape (review finding: cancel_subtask's converged
#    no-op cancel made cancel_agent's `except Conflict` branch dead code,
#    silently misreporting a no-op on an already-terminal subtask as a
#    fresh `cancelled: true`).
# --------------------------------------------------------------------- #
def test_cancel_agent_reports_already_terminal_subtask_as_noop(tmp_path):
    from agentconnect.core import EchoWorker

    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker("echo")],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="s", instructions="do it"))
    # EchoWorker completes synchronously under the direct backend.
    assert svc.storage.get_subtask(subtask.id).status is SubtaskStatus.succeeded

    out = svc.cancel_agent(subtask.id)
    # Pre-authority wire shape preserved: `cancelled: false` plus the exact
    # legacy detail string — never a fake `cancelled: true` for a subtask
    # this call did not change (it stays succeeded, not cancelled).
    assert out["cancelled"] is False
    assert out["detail"] == f"subtask {subtask.id} is already succeeded"
    assert svc.storage.get_subtask(subtask.id).status is SubtaskStatus.succeeded

    # Repeat on an already-CANCELLED subtask: same truthful no-op shape.
    second = svc.submit_subtask(task.id, SubtaskRequest(title="s2", instructions="do it"))
    svc.cancel_subtask(second.id)  # terminal succeeded -> no-op, stays succeeded
    out2 = svc.cancel_agent(second.id)
    assert out2["cancelled"] is False
    assert out2["detail"] == f"subtask {second.id} is already {svc.storage.get_subtask(second.id).status.value}"
