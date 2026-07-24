"""The ToolConnect governor is really consulted, and fails closed (HIGH fix).

Before this fix the governor was configurable (`bind_tool_governor`,
`toolconnect_governor_from_env`) but consulted on NO execution path: a dead seam
that enforced nothing, while `authorize()` mislabelled every task-scoped action as a
`tool.authorized` event. These regressions pin the real behaviour:

* worker preparation (`_execute`) authorizes the worker's DECLARED tool set through
  the bound governor before any worker spawns;
* a policy deny OR an engine outage (a governor that raises) refuses the subtask
  fail-closed — the worker never runs, no artifact, no run row;
* an allowed set proceeds, fires a distinct `tool.authorized` event carrying the
  ToolConnect `decision_id`, and records the outcome best-effort;
* with no governor bound, behaviour is byte-for-byte what it was — and no
  `tool.authorized` event is fabricated;
* the generic action gate now emits `action.authorized`, not `tool.authorized`.
"""

from __future__ import annotations

import pytest

from agentconnect.core import (
    AgentConnectService,
    ArtifactType,
    CreateTaskRequest,
    EchoWorker,
    PolicyViolation,
    PrivacyTier,
    SubtaskRequest,
    SubtaskStatus,
    Unauthenticated,
    WorkerAdapter,
    WorkerCapabilities,
    WorkerLocation,
    WorkerResult,
)
from agentconnect.core.observability import (
    CompositeObservabilityProvider,
    ObservabilityEmitter,
    StructuredLogObservabilityProvider,
)
from agentconnect.core.toolconnect_client import ToolDecision, ToolUseAuthorization
from agentconnect.core.workers import WorkerArtifactRef


# ------------------------------------------------------------------- test doubles
class FakeGovernor:
    """An in-process `ToolGovernor`. Deterministic, no HTTP — the network path is
    already covered by `test_toolconnect_client`; here we pin the *consultation*."""

    def __init__(self, *, deny=(), raise_on=(), mode="required"):
        self.mode = mode
        self.deny = set(deny)
        self.raise_on = set(raise_on)
        self.calls: list[tuple] = []
        self.records: list[tuple] = []

    def authorize(self, principal, source_id, name, context=None):
        self.calls.append((source_id, name, dict(principal), dict(context or {})))
        if name in self.raise_on:
            raise RuntimeError("engine exploded")
        if name in self.deny:
            return ToolDecision(
                allowed=False, reason=f"policy forbids {name}",
                decision_id=f"dec-{name}", default_deny=False,
                determining_policies=(f"no-{name}",), contract_version="1.0",
            )
        return ToolDecision(
            allowed=True, reason="allowed", decision_id=f"dec-{name}",
            determining_policies=(f"allow-{name}",), contract_version="1.0",
        )

    def record(self, decision_id, outcome, detail=None):
        self.records.append((decision_id, outcome, dict(detail or {})))
        return {"recorded": True}

    def health(self):
        return {"status": "ok"}


class NeverRunWorker(WorkerAdapter):
    """Routable, but its `run` must never be reached: if the governor denies the
    declared tool set the worker is refused *before* spawn. A reached `run` is a bug."""

    def __init__(self, tools):
        self._tools = list(tools)

    @property
    def worker_id(self) -> str:
        return "never_run_worker"

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id="never_run_worker", harness="demo_harness", tools=self._tools,
            privacy_tiers=list(PrivacyTier),
            capability_tags=["echo", "inspect", "summarize", "generate"],
            location=WorkerLocation.local,
        )

    def run(self, subtask, context) -> WorkerResult:  # pragma: no cover - must not run
        raise AssertionError("worker ran despite a governor deny")


class ToolWorker(WorkerAdapter):
    """A worker with a declared multi-tool set that DOES run when authorized."""

    def __init__(self, tools, location=WorkerLocation.local):
        self._tools = list(tools)
        self._location = location
        self.ran = False

    @property
    def worker_id(self) -> str:
        return "tool_worker"

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id="tool_worker", harness="demo_harness", tools=self._tools,
            privacy_tiers=list(PrivacyTier),
            capability_tags=["echo", "inspect", "summarize", "generate"],
            location=self._location,
        )

    def run(self, subtask, context) -> WorkerResult:
        self.ran = True
        art = context.create_artifact(
            type=ArtifactType.worker_output, content="did work", summary="s")
        return WorkerResult(
            status="succeeded", summary="ran",
            artifacts=[WorkerArtifactRef(artifact_id=art.id)])


# --------------------------------------------------------------------- fixtures
def _service(tmp_path, workers, *, observe=True):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=workers)
    if observe:
        prov = StructuredLogObservabilityProvider(tmp_path / "events.jsonl")
        comp = CompositeObservabilityProvider([prov])
        svc.bind_observability(ObservabilityEmitter(comp, redactor=svc.observation_redactor()))
    return svc


def _kinds(svc, task_id):
    return [e["event_type"] for e in svc.observation_events(task_id=task_id)]


def _events(svc, task_id, kind):
    return [e for e in svc.observation_events(task_id=task_id) if e["event_type"] == kind]


# --------------------------------------------------------------- no governor bound
def test_no_governor_runs_unchanged_and_fabricates_no_tool_authorized(tmp_path):
    svc = _service(tmp_path, [EchoWorker()])
    task = svc.create_task(CreateTaskRequest(title="T"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    assert sub.status is SubtaskStatus.succeeded
    # Standalone: no governor => no tool authorization is *claimed*.
    assert "tool.authorized" not in _kinds(svc, task.id)


def test_consult_is_a_permissive_noop_without_a_governor(tmp_path):
    svc = _service(tmp_path, [EchoWorker()], observe=False)
    authz = svc._consult_tool_governor(
        ["write_artifact"], source_id="echo",
        principal={"id": "x", "kind": "agent", "privacy_tier": "local"})
    assert authz == ToolUseAuthorization(allowed=True, governed=False)


# ----------------------------------------------------------- governor allows a set
def test_allowed_declared_set_runs_and_fires_a_real_decision(tmp_path):
    gov = FakeGovernor()
    svc = _service(tmp_path, [ToolWorker(["generate", "write_artifact"])])
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))

    assert sub.status is SubtaskStatus.succeeded
    # Every declared tool was consulted, under the worker's harness as source.
    assert {(s, n) for s, n, *_ in gov.calls} == {
        ("demo_harness", "generate"), ("demo_harness", "write_artifact")}
    ta = _events(svc, task.id, "tool.authorized")
    assert len(ta) == 2
    assert all(e["metadata"]["decision_id"].startswith("dec-") for e in ta)
    assert all(e["metadata"]["allowed"] is True for e in ta)
    # Outcome recorded best-effort as a grant (not a fabricated invocation result).
    assert {o for _, o, _ in gov.records} == {"authorized"}
    # A local worker authorizes as a "local" principal — passed through unchanged.
    assert all(p["privacy_tier"] == "local" for _, _, p, _ in gov.calls)


def test_cloud_worker_principal_uses_toolconnects_trusted_cloud_tier(tmp_path):
    # ToolConnect's tier vocabulary is {local, trusted-cloud, rented}; a raw "cloud"
    # would rank as unknown (worst) and be unmatchable by Cedar policies written
    # against "trusted-cloud". The chokepoint must translate the one mismatched value.
    gov = FakeGovernor()
    svc = _service(tmp_path, [ToolWorker(["generate"], location=WorkerLocation.cloud)],
                   observe=False)
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))

    assert sub.status is SubtaskStatus.succeeded
    assert gov.calls and all(p["privacy_tier"] == "trusted-cloud"
                             for _, _, p, _ in gov.calls)


# ------------------------------------------------------------ governor denies a tool
def test_policy_deny_blocks_the_subtask_before_the_worker_runs(tmp_path):
    gov = FakeGovernor(deny={"danger"})
    worker = NeverRunWorker(["read_notes", "danger"])
    svc = _service(tmp_path, [worker])
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    # NeverRunWorker.run raises if reached; a clean failed subtask proves it wasn't.
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))

    assert sub.status is SubtaskStatus.failed
    assert svc.get_subtask(sub.id).runs == []          # no worker run was ever created
    assert sub.result_artifact_id is None              # nothing was produced
    denied = _events(svc, task.id, "subtask.denied")
    assert denied and denied[-1]["metadata"]["denied_tool"] == "demo_harness:danger"
    assert denied[-1]["metadata"]["unavailable"] is False   # a policy deny, not an outage
    # The block is recorded to the governor's audit as a block, never an allow.
    assert ("dec-danger", "blocked") in {(d, o) for d, o, _ in gov.records}


def test_deny_is_durable_as_a_failed_attempt(tmp_path):
    gov = FakeGovernor(deny={"danger"})
    svc = _service(tmp_path, [NeverRunWorker(["danger"])])
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    attempts = svc.get_task(task.id).attempts
    assert attempts and attempts[-1].outcome == "failed"
    assert "denied by governor" in attempts[-1].summary


# ------------------------------------------------------ governor outage => fail closed
def test_unreachable_governor_fails_closed(tmp_path):
    # A governor that raises stands in for an unreachable/garbled engine.
    gov = FakeGovernor(raise_on={"read_notes"})
    svc = _service(tmp_path, [NeverRunWorker(["read_notes"])])
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))

    assert sub.status is SubtaskStatus.failed          # outage NEVER becomes an allow
    denied = _events(svc, task.id, "subtask.denied")
    assert denied and denied[-1]["metadata"]["unavailable"] is True


# ---------------------------------------------------------------- event reconciliation
def test_action_gate_emits_action_authorized_not_tool_authorized(tmp_path):
    svc = _service(tmp_path, [EchoWorker()])
    task = svc.create_task(CreateTaskRequest(title="T"))
    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    svc.authorize(launched["token"], "get_status", task_id=task.id)
    kinds = _kinds(svc, task.id)
    assert "action.authorized" in kinds
    # The generic token/scope gate must no longer masquerade as a tool authorization.
    # (No governor is bound here, so nothing legitimately emits tool.authorized.)
    assert "tool.authorized" not in kinds


# ------------------------------------------------------- explicit authorize_tool_use
def test_authorize_tool_use_enforces_the_token_gate(tmp_path):
    svc = _service(tmp_path, [EchoWorker()], observe=False)
    svc.bind_tool_governor(FakeGovernor())
    with pytest.raises(Unauthenticated):
        svc.authorize_tool_use("not-a-real-token", ["write_artifact"])


def test_authorize_tool_use_routes_a_valid_token_to_the_governor(tmp_path):
    gov = FakeGovernor(deny={"danger"})
    svc = _service(tmp_path, [EchoWorker()], observe=False)
    svc.bind_tool_governor(gov)
    task = svc.create_task(CreateTaskRequest(title="T"))
    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    allowed = svc.authorize_tool_use(
        launched["token"], ["read_notes"], source_id="demo", task_id=task.id)
    denied = svc.authorize_tool_use(
        launched["token"], ["danger"], source_id="demo", task_id=task.id)
    assert allowed.allowed is True and allowed.governed is True
    assert denied.allowed is False and denied.denied_tool == "demo:danger"


def test_a_manager_token_scoped_to_another_task_cannot_authorize_tools(tmp_path):
    svc = _service(tmp_path, [EchoWorker()], observe=False)
    svc.bind_tool_governor(FakeGovernor())
    task_a = svc.create_task(CreateTaskRequest(title="A"))
    task_b = svc.create_task(CreateTaskRequest(title="B"))
    launched = svc.launch_session("claude", task_id=task_a.id, claim=True)
    with pytest.raises(PolicyViolation):
        svc.authorize_tool_use(launched["token"], ["x"], task_id=task_b.id)
