"""The ecosystem event bus: `event_log`, its two producer paths, and the
`GET /events` + `GET /events/stream` HTTP surface (docs/EVENT_BUS.md).

Two producer paths, tested separately per the architecture (§0):

* Path 1 — structural, same-commit, fail-closed: `state.changed`, written by
  `SqliteStorage._insert_transition_audit` for EVERY applied transition,
  independent of whether any observability provider is configured.
* Path 2 — rich, ordered-after, advisory: every existing `_observe(...)` site,
  persisted by the always-on `SqliteEventLogProvider`.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from conftest import operator_client  # noqa: E402

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    PrivacyTier,
    RawModelWorker,
    RoutePolicy,
    SubtaskRequest,
    SubtaskStatus,
    TrustedMemoryAdapter,
    WorkerLocation,
)
from agentconnect.core.memory import CaptureRequest, CaptureResult, RecallPack, RecallRequest
from agentconnect.core.observability import (
    CompositeObservabilityProvider,
    NoopObservabilityProvider,
    ObservabilityEmitter,
)
from agentconnect.api.routes_events import sse_lines


# --------------------------------------------------------------------- fixtures
def _svc(tmp_path, **kwargs) -> AgentConnectService:
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "art"),
        workers=kwargs.pop("workers", [EchoWorker()]), **kwargs,
    )


def _kinds(svc: AgentConnectService, **filters) -> list[str]:
    return [e["type"] for e in svc.list_bus_events(limit=500, **filters)]


class _RecordingAuthority(TrustedMemoryAdapter):
    @property
    def backend_name(self) -> str:
        return "wikibrain"

    def recall(self, request: RecallRequest) -> RecallPack:  # pragma: no cover
        return RecallPack(profile=request.profile, query=request.query, items=[])

    def capture_candidate(self, request: CaptureRequest) -> CaptureResult:  # pragma: no cover
        return CaptureResult(accepted=True, candidate_id="cand_1", backend=self.backend_name)

    def promote_candidate(self, candidate_id: str, promoted_by: str,
                          confidence: Optional[str] = None, scope: Optional[str] = None,
                          safety_override: bool = False,
                          override_reason: Optional[str] = None) -> dict[str, Any]:
        return {"claim_id": candidate_id, "status": "promoted"}


class _FlakyMemoryAdapter:
    """A minimal `MemoryAdapter`-shaped double whose `health()` status is
    controlled by the test — for edge-triggered provider.offline/recovered."""

    backend_name = "flaky"

    def __init__(self) -> None:
        self.status = "unreachable"

    def recall(self, request):  # pragma: no cover
        from agentconnect.core.memory import RecallPack
        return RecallPack(profile=request.profile, query=request.query, items=[])

    def capture_candidate(self, request):  # pragma: no cover
        from agentconnect.core.memory import CaptureResult
        return CaptureResult(accepted=False, backend=self.backend_name)

    def health(self) -> dict[str, Any]:
        return {"backend": self.backend_name, "status": self.status}


# ---------------------------------------------------------- Path 1: state.changed
def test_state_changed_is_written_even_with_no_provider_configured(tmp_path):
    """Structural invariant: strip the emitter down to a noop-only composite
    (Path 2 dark) and drive a real transition — the `state.changed` skeleton
    still lands, because it is written by storage, not by the emitter."""
    svc = _svc(tmp_path)
    svc.bind_observability(ObservabilityEmitter(
        CompositeObservabilityProvider([NoopObservabilityProvider()])))
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    assert sub.status is SubtaskStatus.succeeded
    changed = [e for e in svc.list_bus_events(limit=500) if e["type"] == "state.changed"]
    assert changed, "state.changed must be written independent of any provider"
    subtask_transitions = [e for e in changed if e["subtask_id"] == sub.id]
    assert any(e["payload"]["dst"] == "succeeded" for e in subtask_transitions)


# ---------------------------------------------------------------------- replay
def test_replay_from_seq_zero_reconstructs_the_run(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    assert sub.status is SubtaskStatus.succeeded

    events = svc.list_bus_events(since=0, limit=500)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq must be strictly increasing"
    kinds = [e["type"] for e in events]
    for expected in (
        "task.created", "subtask.created", "state.changed",
        "worker.completed", "subtask.completed",
    ):
        assert expected in kinds, f"{expected} missing from replay: {kinds}"
    # causal order: created strictly before completed
    assert kinds.index("subtask.created") < kinds.index("subtask.completed")


def test_since_is_exclusive_and_resumable(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    first_batch = svc.list_bus_events(limit=500)
    cursor = first_batch[-1]["seq"]
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    resumed = svc.list_bus_events(since=cursor, limit=500)
    assert all(e["seq"] > cursor for e in resumed)
    assert resumed  # something new arrived
    assert svc.list_bus_events(since=svc.latest_bus_seq(), limit=500) == []


def test_limit_type_task_id_and_outcome_filters(tmp_path):
    svc = _svc(tmp_path)
    task_a = svc.create_task(CreateTaskRequest(title="A", created_by="human"))
    task_b = svc.create_task(CreateTaskRequest(title="B", created_by="human"))
    svc.submit_subtask(task_a.id, SubtaskRequest(title="d", instructions="x"))
    svc.submit_subtask(task_b.id, SubtaskRequest(title="d", instructions="x"))

    only_created = svc.list_bus_events(types=["task.created"], limit=500)
    assert {e["task_id"] for e in only_created} == {task_a.id, task_b.id}

    only_a = svc.list_bus_events(task_id=task_a.id, limit=500)
    assert only_a and all(e["task_id"] == task_a.id for e in only_a)

    only_succeeded = svc.list_bus_events(types=["worker.completed"], outcome="succeeded",
                                         limit=500)
    assert only_succeeded and all(e["outcome"] == "succeeded" for e in only_succeeded)

    capped = svc.list_bus_events(limit=1)
    assert len(capped) == 1
    assert len(svc.list_bus_events(limit=10_000)) <= 500  # clamped server-side


def test_latest_seq_tracks_the_highest_seq_written(tmp_path):
    svc = _svc(tmp_path)
    assert svc.latest_bus_seq() == 0
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    latest = svc.latest_bus_seq()
    assert latest > 0
    assert svc.list_bus_events(limit=500)[-1]["seq"] == latest


# --------------------------------------------------------- failure/cancel/reap
def test_cancel_task_and_cancel_subtask_emit_with_state_changed(tmp_path):
    # A cost-capped cloud worker parks the subtask in `needs_approval` — a real,
    # non-terminal status `cancel_subtask` can actually still act on (a
    # DirectExecutionBackend echo worker would already be terminal by the time
    # `cancel_subtask` runs, since it completes synchronously inside `submit_subtask`).
    cloud = RawModelWorker("cloud", lambda p: "out", model="gpt",
                           location=WorkerLocation.cloud,
                           privacy_tiers=[PrivacyTier.public],
                           cost_per_1k_tokens_usd=5.0)
    svc = _svc(tmp_path, workers=[cloud], policy=RoutePolicy(max_cost_usd=0.01))
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    assert sub.status is SubtaskStatus.needs_approval

    svc.cancel_subtask(sub.id)
    svc.cancel_task(task.id)
    kinds = _kinds(svc, task_id=task.id)
    assert "subtask.cancelled" in kinds
    assert "task.cancelled" in kinds
    assert "state.changed" in kinds


def test_expire_approval_emits_and_fails_the_subtask(tmp_path):
    cloud = RawModelWorker("cloud", lambda p: "out", model="gpt",
                           location=WorkerLocation.cloud,
                           privacy_tiers=[PrivacyTier.public],
                           cost_per_1k_tokens_usd=5.0)
    svc = _svc(tmp_path, workers=[cloud], policy=RoutePolicy(max_cost_usd=0.01))
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    assert sub.status is SubtaskStatus.needs_approval
    approval = svc.storage.pending_approval_for(sub.id)
    assert approval is not None
    svc.expire_approval(approval.id)
    assert svc.get_subtask(sub.id).subtask.status is SubtaskStatus.failed
    kinds = _kinds(svc, task_id=task.id)
    assert "approval.expired" in kinds


def test_dependency_cascade_via_failed_worker_emits_subtask_failed(tmp_path):
    # A cost-capped cloud worker whose `generate` raises: the blocker parks in
    # `needs_approval` (still non-terminal, so a dependent submitted now really
    # is blocked on it, not on an already-failed id) and only fails once
    # approved and actually run — which is what lets `_cascade_dependency_failure`
    # (only reachable from a worker's own failure) do real work here.
    def _boom(prompt: str) -> str:
        raise RuntimeError("boom")

    cloud = RawModelWorker("cloud", _boom, model="gpt", location=WorkerLocation.cloud,
                           privacy_tiers=[PrivacyTier.public], cost_per_1k_tokens_usd=5.0)
    svc = _svc(tmp_path, workers=[cloud], policy=RoutePolicy(max_cost_usd=0.01))
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    blocker = svc.submit_subtask(task.id, SubtaskRequest(
        title="blocker", instructions="x", privacy_tier=PrivacyTier.public))
    assert blocker.status is SubtaskStatus.needs_approval

    dependent = svc.submit_subtask(
        task.id, SubtaskRequest(title="dep", instructions="y", depends_on=[blocker.id]))
    assert dependent.status is SubtaskStatus.blocked

    svc.approve_subtask(blocker.id, "operator")
    assert svc.get_subtask(blocker.id).subtask.status is SubtaskStatus.failed
    assert svc.get_subtask(dependent.id).subtask.status is SubtaskStatus.failed

    kinds = _kinds(svc, task_id=task.id)
    assert kinds.count("subtask.failed") >= 2  # blocker's own + the cascade


def test_cascade_lost_race_emits_no_unbacked_subtask_failed(tmp_path, monkeypatch):
    """Two callers can race `_cascade_dependency_failure` for the same blocked
    dependent (worker-failure, governor-deny, and reap paths all cascade). The
    loser's CAS lands as a noop — it must NOT emit a rich `subtask.failed`,
    which would put an event on the bus with no same-commit `state.changed`
    sibling (docs/EVENT_BUS.md §3: extra rich events are never allowed, only
    crash-lost ones). Simulated deterministically: `advance()` returning None
    is exactly what the loser of the blocked->failed race observes."""
    cloud = RawModelWorker("cloud", lambda p: "out", model="gpt",
                           location=WorkerLocation.cloud,
                           privacy_tiers=[PrivacyTier.public],
                           cost_per_1k_tokens_usd=5.0)
    svc = _svc(tmp_path, workers=[cloud], policy=RoutePolicy(max_cost_usd=0.01))
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    blocker = svc.submit_subtask(task.id, SubtaskRequest(
        title="blocker", instructions="x", privacy_tier=PrivacyTier.public))
    assert blocker.status is SubtaskStatus.needs_approval
    dependent = svc.submit_subtask(
        task.id, SubtaskRequest(title="dep", instructions="y", depends_on=[blocker.id]))
    assert dependent.status is SubtaskStatus.blocked

    before = _kinds(svc, task_id=task.id).count("subtask.failed")
    monkeypatch.setattr(svc._subtask_status_authority, "advance",
                        lambda *a, **k: None)  # the loser's view of the CAS
    cascaded = svc._cascade_dependency_failure(task.id, blocker.id)
    assert cascaded == []
    assert _kinds(svc, task_id=task.id).count("subtask.failed") == before


# --------------------------------------------------------------- memory.promoted
def test_memory_promoted_carries_no_claim_content(tmp_path):
    svc = _svc(tmp_path, memory_backends={"wikibrain": _RecordingAuthority()})
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.promote_memory_candidate("cand_1", "operator")
    events = [e for e in svc.list_bus_events(limit=500) if e["type"] == "memory.promoted"]
    assert events
    payload = events[-1]["payload"]
    assert payload.get("claim_id") == "cand_1"
    blob = str(payload)
    assert "cand_1" in blob  # the id itself is fine
    # No key here carries prose/content — just ids/lists/enums.
    assert set(payload.keys()) <= {
        "claim_id", "indexed_into", "index_failures", "agent_id", "agent_role", "provider",
    }


# ---------------------------------------------------- provider.offline/recovered
def test_provider_health_transitions_are_edge_triggered(tmp_path):
    flaky = _FlakyMemoryAdapter()
    svc = _svc(tmp_path, memory_backends={"flaky": flaky})

    svc.readiness()
    offline = _kinds(svc, types=["provider.offline"])
    assert len(offline) == 1

    svc.readiness()  # unchanged status -> no repeat emission
    assert len(_kinds(svc, types=["provider.offline"])) == 1

    flaky.status = "ok"
    svc.readiness()
    recovered = _kinds(svc, types=["provider.recovered"])
    assert len(recovered) == 1

    svc.readiness()  # still ok -> no repeat
    assert len(_kinds(svc, types=["provider.recovered"])) == 1


def test_healthy_from_the_start_emits_no_baseline_noise(tmp_path):
    class HealthyAdapter(_FlakyMemoryAdapter):
        def __init__(self):
            super().__init__()
            self.status = "ok"

    svc = _svc(tmp_path, memory_backends={"h": HealthyAdapter()})
    svc.readiness()
    assert _kinds(svc, types=["provider.offline", "provider.degraded", "provider.recovered"]) == []


# ------------------------------------------------------------- tool.authorized
def test_tool_authorized_durable_and_denied_outcome_queryable(tmp_path):
    from agentconnect.core.toolconnect_client import ToolDecision
    from agentconnect.core.workers import WorkerAdapter, WorkerCapabilities, WorkerResult

    class FakeGovernor:
        def authorize(self, principal, source_id, name, context=None, **kw):
            if name == "danger":
                return ToolDecision(allowed=False, reason="no", decision_id="dec-danger",
                                    default_deny=False, determining_policies=("no-danger",),
                                    contract_version="1.1")
            return ToolDecision(allowed=True, reason="ok", decision_id=f"dec-{name}",
                                determining_policies=(f"allow-{name}",), contract_version="1.1")

        def record(self, *a, **kw):
            return None

    class ToolWorker(WorkerAdapter):
        @property
        def worker_id(self) -> str:
            return "tool_worker"

        def capabilities(self) -> WorkerCapabilities:
            return WorkerCapabilities(
                worker_id="tool_worker", harness="demo", tools=["danger"],
                privacy_tiers=list(PrivacyTier), capability_tags=["echo"],
                location=WorkerLocation.local,
            )

        def run(self, subtask, context) -> WorkerResult:  # pragma: no cover
            raise AssertionError("must not run: governor denies its only tool")

    svc = _svc(tmp_path, workers=[ToolWorker()])
    svc.bind_tool_governor(FakeGovernor())
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    assert sub.status is SubtaskStatus.failed

    denied = svc.list_bus_events(types=["tool.authorized"], outcome="denied", limit=500)
    assert denied
    assert denied[-1]["payload"]["decision_id"] == "dec-danger"


# ------------------------------------------------------------------ durability
def test_seq_durability_across_reopen_and_event_id_dedup(tmp_path):
    from agentconnect.core.storage import SqliteStorage

    db_path = str(tmp_path / "ledger.db")
    storage = SqliteStorage(db_path)
    seq1 = storage.append_bus_event(event_id="ev-1", type="task.created", actor="x")
    dup = storage.append_bus_event(event_id="ev-1", type="task.created", actor="x")
    assert dup is None
    storage.close()

    reopened = SqliteStorage(db_path)
    seq2 = reopened.append_bus_event(event_id="ev-2", type="task.created", actor="x")
    assert seq2 > seq1
    assert reopened.latest_bus_seq() == seq2


# -------------------------------------------------------------- passive guard
def test_default_event_log_provider_writes_no_observation_handles(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr", task_id=task.id)
    sid = launched["session"].id
    rows = svc.storage.observation_handles_for("session", sid)
    assert all(r["provider"] != "event_log" for r in rows)


# ---------------------------------------------------------------- SSE framing
def test_sse_generator_frames_ids_types_and_resumes(tmp_path):
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))

    collected = []
    ticks = [0]

    def stop_after_first_batch():
        ticks[0] += 1
        return len(collected) >= 2 or ticks[0] > 50

    for chunk in sse_lines(svc, 0, None, 0.001, should_stop=stop_after_first_batch):
        if chunk.startswith("id:"):
            collected.append(chunk)
    assert collected
    assert collected[0].startswith("id: 1\n")
    assert '"type": "task.created"' in collected[0]

    # Resume from the last seq seen: no duplicate frames.
    last_seq = svc.latest_bus_seq()
    svc.create_task(CreateTaskRequest(title="T2", created_by="human"))
    more = []
    ticks2 = [0]

    def stop_after_second():
        ticks2[0] += 1
        return len(more) >= 1 or ticks2[0] > 50

    for chunk in sse_lines(svc, last_seq, None, 0.001, should_stop=stop_after_second):
        if chunk.startswith("id:"):
            more.append(chunk)
    assert more
    assert '"type": "task.created"' in more[0]
    assert "T2" in more[0]


def test_sse_type_filter_only_yields_matching_events(tmp_path):
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))

    collected = []
    ticks = [0]

    def stop():
        ticks[0] += 1
        return ticks[0] > 3  # a few polls with nothing matching, then give up

    for chunk in sse_lines(svc, 0, ["subtask.created"], 0.001, should_stop=stop):
        if chunk.startswith("id:"):
            collected.append(chunk)
    assert collected == []  # only task.created exists so far; filter excludes it


# ----------------------------------------------------------------------- auth
def _manager_token(svc, task_id: str) -> str:
    return svc.launch_session("claude", task_id=task_id, claim=True)["token"]


def test_events_requires_operator_scope(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    mgr = _manager_token(svc, task.id)
    client = operator_client(svc)

    anon = client.__class__(client.app)
    assert anon.get("/events").status_code == 401
    assert anon.get("/events/stream").status_code == 401

    manager_client = client.__class__(client.app)
    manager_client.headers.update({"Authorization": f"Bearer {mgr}"})
    assert manager_client.get("/events").status_code == 403
    assert manager_client.get("/events/stream").status_code == 403

    resp = client.get("/events")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body and "latest_seq" in body
    assert any(e["type"] == "task.created" for e in body["events"])


def test_events_unknown_type_is_400(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    resp = client.get("/events", params={"type": "not.a.real.type"})
    assert resp.status_code == 400


# ----------------------------------------------------------- SSE end-to-end
def _bounded_sse_client(svc, polls: int = 3):
    """An operator TestClient whose SSE route runs a BOUNDED stream: the
    injectable `sse_should_stop` seam ends the generator after `polls` poll
    loops, because an in-process TestClient cannot safely half-close an
    infinite streaming response (the read side and the ASGI portal share one
    thread of control — breaking out mid-stream deadlocks)."""
    client = operator_client(svc)
    client.app.state.sse_poll_interval = 0.01

    # A fresh predicate per request, so one test can open several streams.
    def arm(polls_override: int = polls) -> None:
        seen = {"n": 0}

        def _stop() -> bool:
            seen["n"] += 1
            return seen["n"] > polls_override

        client.app.state.sse_should_stop = _stop

    arm()
    client.arm_sse = arm  # type: ignore[attr-defined]
    return client


def test_sse_stream_success_path_through_the_real_app(tmp_path):
    """`GET /events/stream` exercised through the actual ASGI app with an
    operator token — the route handler itself (cursor selection, poll-interval
    injection, StreamingResponse headers), not just the extracted generator."""
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    client = _bounded_sse_client(svc)

    with client.stream("GET", "/events/stream", params={"since": 0}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        lines = list(resp.iter_lines())

    assert lines[0] == "retry: 3000"
    assert any(l.startswith("id: ") for l in lines)
    assert any(l == "event: task.created" for l in lines)
    data = [json.loads(l[len("data: "):]) for l in lines if l.startswith("data: ")]
    assert len(data) >= 2 and data[0]["seq"] >= 1
    seqs = [d["seq"] for d in data]
    assert seqs == sorted(seqs)


def test_sse_stream_last_event_id_resume_and_since_priority(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    all_events = svc.list_bus_events(since=0, limit=500)
    assert len(all_events) >= 3
    first_seq = all_events[0]["seq"]

    client = _bounded_sse_client(svc)

    # Last-Event-ID resumes strictly after the given seq.
    with client.stream("GET", "/events/stream",
                       headers={"Last-Event-ID": str(first_seq)}) as resp:
        assert resp.status_code == 200
        lines = list(resp.iter_lines())
    data = [json.loads(l[len("data: "):]) for l in lines if l.startswith("data: ")]
    assert data and data[0]["seq"] == all_events[1]["seq"]

    # An explicit ?since= wins over a contradictory Last-Event-ID header.
    client.arm_sse()
    with client.stream("GET", "/events/stream", params={"since": 0},
                       headers={"Last-Event-ID": "999999"}) as resp:
        lines = list(resp.iter_lines())
    data = [json.loads(l[len("data: "):]) for l in lines if l.startswith("data: ")]
    assert data and data[0]["seq"] == first_seq

    # A non-integer Last-Event-ID falls back to live-tail (latest_bus_seq):
    # nothing new arrives, so the stream carries no data frames at all.
    client.arm_sse(20)  # enough idle polls to cross the keepalive threshold
    with client.stream("GET", "/events/stream",
                       headers={"Last-Event-ID": "not-a-number"}) as resp:
        lines = list(resp.iter_lines())
    assert lines[0] == "retry: 3000"
    assert not any(l.startswith("data: ") for l in lines)
    assert any(l.startswith(": keepalive") for l in lines)


def test_sse_stream_unknown_type_filter_is_400_through_the_app(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    resp = client.get("/events/stream", params={"type": "bogus.type"})
    assert resp.status_code == 400


# ------------------------------------------- governor deny reason tier-gating
def test_governor_deny_reason_withheld_for_strict_tiers(tmp_path):
    """The tool-governor's free-text policy reason is tier-gated on the bus
    like every other free-text field; the tool identifiers still ride (a
    consumer learns WHICH tool was denied, never the prose why)."""
    from agentconnect.core.toolconnect_client import ToolDecision
    from agentconnect.core.workers import WorkerAdapter, WorkerCapabilities, WorkerResult

    canary = "CANARY_policy_reason_d41 the secret project forbids it"

    class FakeGovernor:
        def authorize(self, principal, source_id, name, context=None, **kw):
            return ToolDecision(allowed=False, reason=canary, decision_id="dec-1",
                                default_deny=False, determining_policies=("p",),
                                contract_version="1.1")

        def record(self, *a, **kw):
            return None

    class ToolWorker(WorkerAdapter):
        @property
        def worker_id(self) -> str:
            return "tool_worker"

        def capabilities(self) -> WorkerCapabilities:
            return WorkerCapabilities(
                worker_id="tool_worker", harness="demo", tools=["danger"],
                privacy_tiers=list(PrivacyTier), capability_tags=["echo"],
                location=WorkerLocation.local,
            )

        def run(self, subtask, context) -> WorkerResult:  # pragma: no cover
            raise AssertionError("must not run: governor denies its only tool")

    svc = _svc(tmp_path, workers=[ToolWorker()])
    svc.bind_tool_governor(FakeGovernor())
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.secret_sensitive))
    assert sub.status is SubtaskStatus.failed

    denied = [e for e in svc.list_bus_events(task_id=task.id, limit=500)
              if e["type"] == "subtask.denied"]
    assert denied
    assert denied[-1]["payload"]["reason"] == "[redacted]"
    assert canary not in str(svc.list_bus_events(task_id=task.id, limit=500))
