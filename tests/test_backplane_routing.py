"""Worker routing MVP: echo worker execution, deterministic selection, route
explanation persistence, hard gates, and the approval path (spec §18-§21, §24).

No model, no network. `echo_worker` is the fixture that makes routing testable.
"""

import json

import pytest

from agentconnect.core import (
    AgentConnectService,
    ArtifactType,
    Conflict,
    CreateTaskRequest,
    EchoWorker,
    FilesystemAccess,
    InvalidRequest,
    PrivacyTier,
    RawModelWorker,
    RoutePolicy,
    SandboxSpec,
    SubtaskRequest,
    SubtaskStatus,
    TaskStatus,
    WorkerAdapter,
    WorkerCapabilities,
    WorkerHealth,
    WorkerLocation,
    WorkerResult,
    route,
)
from agentconnect.core.errors import PolicyViolation
from agentconnect.core.routing import WorkerRegistry


def make_service(tmp_path, workers, policy=None):
    return AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "artifacts"),
        workers=workers, policy=policy,
    )


def cloud_worker(worker_id="cheap_cloud_deepseek", cost=1.0, tiers=None, tags=None):
    return RawModelWorker(
        worker_id, lambda prompt: f"cloud says: {prompt[:20]}", model="deepseek-v3",
        location=WorkerLocation.cloud,
        privacy_tiers=tiers or [PrivacyTier.public, PrivacyTier.public_redacted],
        capability_tags=tags or ["generate", "summarize"],
        cost_per_1k_tokens_usd=cost,
    )


def local_model_worker(worker_id="local_qwen_worker", tiers=None, tags=None):
    return RawModelWorker(
        worker_id, lambda prompt: "local output", model="qwen2.5-coder-14b",
        location=WorkerLocation.local,
        privacy_tiers=tiers or list(PrivacyTier),
        capability_tags=tags or ["generate", "inspect"],
        cost_per_1k_tokens_usd=0.0,
    )


class UnhealthyWorker(WorkerAdapter):
    @property
    def worker_id(self) -> str:
        return "sick_worker"

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id="sick_worker", harness="stub", privacy_tiers=list(PrivacyTier),
        )

    def health(self) -> WorkerHealth:
        return WorkerHealth(available=False, detail="GPU fell over")

    def run(self, subtask, context) -> WorkerResult:  # pragma: no cover - never routed
        raise AssertionError("an unhealthy worker must never be selected")


class ExplodingWorker(WorkerAdapter):
    @property
    def worker_id(self) -> str:
        return "exploding_worker"

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id="exploding_worker", harness="stub", privacy_tiers=list(PrivacyTier),
        )

    def run(self, subtask, context) -> WorkerResult:
        raise RuntimeError("harness segfaulted")


@pytest.fixture()
def task_svc(tmp_path):
    svc = make_service(tmp_path, [EchoWorker()])
    task = svc.create_task(CreateTaskRequest(title="Refactor auth", goal="dedupe expiry"))
    return svc, task


# ------------------------------------------------------- echo worker execution
def test_echo_worker_runs_and_produces_an_artifact(task_svc):
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="Find duplicated expiry checks",
        instructions="Inspect auth files and return file paths and line ranges only.",
        privacy_tier=PrivacyTier.repo_sensitive,
    ))
    assert subtask.status is SubtaskStatus.succeeded
    assert subtask.assigned_worker == "echo_worker"
    assert subtask.result_artifact_id

    body = svc.read_artifact_chunk(subtask.result_artifact_id, 0, 8000).content
    assert "Inspect auth files" in body
    assert svc.get_task(task.id).task.status is TaskStatus.in_progress


def test_worker_result_is_recorded_as_an_attempt(task_svc):
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    attempts = svc.get_task(task.id).attempts
    assert attempts[-1].actor_id == "echo_worker"
    assert attempts[-1].actor_type.value == "worker"
    assert attempts[-1].artifact_refs == [subtask.result_artifact_id]


def test_worker_run_record_is_persisted(task_svc):
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    runs = svc.get_subtask(subtask.id).runs
    assert len(runs) == 1
    assert runs[0].harness == "echo" and runs[0].status.value == "succeeded"
    assert runs[0].output_artifact_id == subtask.result_artifact_id


def test_a_crashing_worker_fails_the_subtask_not_the_service(tmp_path):
    svc = make_service(tmp_path, [ExplodingWorker()])
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    assert subtask.status is SubtaskStatus.failed
    run = svc.get_subtask(subtask.id).runs[0]
    assert "segfaulted" in run.error


# --------------------------------------------------- deterministic route choice
def test_local_beats_cloud_even_when_both_are_eligible(tmp_path):
    svc = make_service(
        tmp_path, [cloud_worker(cost=0.0), local_model_worker()],
        policy=RoutePolicy(max_cost_usd=10.0),
    )
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    # The cloud worker is free here, so only privacy_fit (local-first) separates them.
    assert subtask.assigned_worker == "local_qwen_worker"


def test_route_selection_is_deterministic_across_registry_order(tmp_path):
    chosen = set()
    for workers in ([EchoWorker(), local_model_worker()], [local_model_worker(), EchoWorker()]):
        svc = make_service(tmp_path, workers)
        task = svc.create_task(CreateTaskRequest(title="t"))
        subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
        chosen.add(subtask.assigned_worker)
    assert len(chosen) == 1


def test_preferred_worker_breaks_the_tie(tmp_path):
    svc = make_service(tmp_path, [EchoWorker(), local_model_worker()])
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", preferred_worker="local_qwen_worker"))
    assert subtask.assigned_worker == "local_qwen_worker"


# ------------------------------------------------------------------ hard gates
def test_repo_sensitive_task_cannot_use_a_public_cloud_worker(tmp_path):
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.repo_sensitive))

    assert subtask.status is SubtaskStatus.failed
    explanation = svc.explain_route(subtask.id)
    rejected = explanation.rejected_workers[0]
    assert rejected.gate == "privacy_allowed"
    assert "repo_sensitive" in rejected.reason and "cloud" in rejected.reason


def test_capability_mismatch_rejects_a_worker(tmp_path):
    svc = make_service(tmp_path, [local_model_worker(tags=["generate"])])
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", required_capabilities=["run_tests"]))
    explanation = svc.explain_route(subtask.id)
    assert explanation.rejected_workers[0].gate == "capability_match"
    assert "run_tests" in explanation.rejected_workers[0].reason


def test_sandbox_demand_beyond_the_worker_offer_is_rejected(tmp_path):
    svc = make_service(tmp_path, [EchoWorker()])
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i",
        sandbox=SandboxSpec(filesystem=FilesystemAccess.workspace_write, shell=True)))
    assert svc.explain_route(subtask.id).rejected_workers[0].gate == "sandbox_supported"


def test_unhealthy_workers_are_never_selected(tmp_path):
    svc = make_service(tmp_path, [UnhealthyWorker()])
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    explanation = svc.explain_route(subtask.id)
    assert explanation.rejected_workers[0].gate == "healthy"
    assert "GPU fell over" in explanation.rejected_workers[0].reason


def test_budget_gate_rejects_a_worker_over_the_ceiling(tmp_path):
    # requires_approval=False isolates the budget gate from the approval gate.
    pricey = RawModelWorker(
        "pricey", lambda p: "x", model="big", location=WorkerLocation.local,
        privacy_tiers=list(PrivacyTier), cost_per_1k_tokens_usd=100.0,
        requires_approval=False,
    )
    svc = make_service(tmp_path, [pricey], policy=RoutePolicy(max_cost_usd=0.0001))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="a long instruction " * 20))
    explanation = svc.explain_route(subtask.id)
    assert explanation.rejected_workers[0].gate == "budget_allowed"
    assert subtask.status is SubtaskStatus.failed


# ------------------------------------------------ route explanation persistence
def test_route_explanation_is_persisted_inline_and_as_an_artifact(task_svc):
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))

    explanation = svc.explain_route(subtask.id)
    assert explanation.selected_worker == "echo_worker"
    assert explanation.selected_harness == "echo"
    assert set(explanation.score_terms) == {
        "privacy_fit", "cost", "availability", "capability", "preferred"
    }
    assert 0.0 < explanation.total_score <= 1.0

    artifacts = [a for a in svc.list_artifacts(task.id)
                 if a.type is ArtifactType.route_explanation]
    assert len(artifacts) == 1
    stored = json.loads(svc.read_artifact_chunk(artifacts[0].id, 0, 8000).content)
    assert stored["selected_worker"] == "echo_worker"
    assert stored["subtask_id"] == subtask.id


def test_explain_route_on_unknown_subtask_is_not_found(task_svc):
    svc, _ = task_svc
    with pytest.raises(Exception):
        svc.explain_route("subtask_nope")


# ------------------------------------------------------------ approval workflow
def test_cloud_worker_parks_the_subtask_for_human_approval(tmp_path):
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))

    assert subtask.status is SubtaskStatus.needs_approval
    assert svc.get_task(task.id).task.status is TaskStatus.needs_approval
    explanation = svc.explain_route(subtask.id)
    assert explanation.needs_approval
    assert explanation.approval_candidate == "cheap_cloud_deepseek"
    assert explanation.approval_location == "cloud"


def test_approval_reroutes_and_runs_the_subtask(tmp_path):
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))

    approved = svc.approve_subtask(subtask.id, "matthew", max_cost_usd=3.0)
    assert approved.status is SubtaskStatus.succeeded
    assert approved.assigned_worker == "cheap_cloud_deepseek"
    assert svc.get_task(task.id).task.status is TaskStatus.in_progress
    kinds = [e.kind for e in svc.list_events(task.id)]
    assert "subtask_approved" in kinds


def test_approval_ceiling_still_binds(tmp_path):
    svc = make_service(tmp_path, [cloud_worker(cost=50.0)], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="x" * 400, privacy_tier=PrivacyTier.public))
    # Approved, but with a ceiling below the estimate: the budget gate wins.
    result = svc.approve_subtask(subtask.id, "matthew", max_cost_usd=0.0001)
    assert result.status is SubtaskStatus.failed
    assert svc.explain_route(subtask.id).rejected_workers[0].gate == "budget_allowed"


def test_deny_fails_the_subtask_and_settles_the_task(tmp_path):
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))

    denied = svc.deny_subtask(subtask.id, "matthew", "too expensive")
    assert denied.status is SubtaskStatus.failed
    assert svc.get_task(task.id).task.status is TaskStatus.in_progress


def test_approving_a_running_subtask_is_a_conflict(task_svc):
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    with pytest.raises(Conflict):
        svc.approve_subtask(subtask.id, "matthew")


def test_local_free_worker_wins_without_any_approval(tmp_path):
    svc = make_service(
        tmp_path, [cloud_worker(), local_model_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    assert subtask.status is SubtaskStatus.succeeded
    assert subtask.assigned_worker == "local_qwen_worker"


# ------------------------------------------------------- subtask dependencies
def test_dependent_subtask_is_blocked_and_never_dispatched(tmp_path):
    # `required_capabilities=["generate"]` filters echo_worker out of A's
    # routing (it lacks that tag), so only the cloud worker is eligible — and
    # a cloud worker always needs approval, which keeps A open (not succeeded)
    # long enough to submit a dependent against it.
    svc = make_service(
        tmp_path, [cloud_worker(), EchoWorker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    a = svc.submit_subtask(task.id, SubtaskRequest(
        title="A", instructions="i", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"],
    ))
    assert a.status is SubtaskStatus.needs_approval  # not yet succeeded

    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.blocked
    assert b.depends_on == [a.id]
    assert b.metadata.get("blocked_on") == [a.id]
    # Never dispatched: no worker run, no execution handle.
    assert svc.storage.list_runs(b.id) == []
    assert svc.executions_for("subtask", b.id) == []
    assert "subtask_blocked" in [e.kind for e in svc.list_events(task.id)]


def test_dependent_subtask_releases_and_runs_once_dependency_succeeds(tmp_path):
    svc = make_service(
        tmp_path, [cloud_worker(), EchoWorker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    a = svc.submit_subtask(task.id, SubtaskRequest(
        title="A", instructions="i", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"],
    ))
    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.blocked

    approved = svc.approve_subtask(a.id, "matthew", max_cost_usd=3.0)
    assert approved.status is SubtaskStatus.succeeded

    # B was released and actually ran (via echo_worker, the only eligible
    # worker with no `required_capabilities` gate) — not merely relabeled.
    released = svc.get_subtask(b.id).subtask
    assert released.status is SubtaskStatus.succeeded
    assert released.assigned_worker == "echo_worker"
    assert "blocked_on" not in released.metadata
    assert svc.storage.list_runs(b.id)

    # Order proof: blocked, then A approved, then B released — in that order.
    # `list_events` returns newest-first, so oldest-first is the reverse.
    kinds = [e.kind for e in reversed(svc.list_events(task.id))]
    assert (
        kinds.index("subtask_blocked")
        < kinds.index("subtask_approved")
        < kinds.index("subtask_released")
    )


def test_depends_on_already_satisfied_dispatches_immediately(task_svc):
    """A dependency that already succeeded before the dependent is submitted
    is not a wait at all — the dependent runs right away, same as `queued`."""
    svc, task = task_svc
    a = svc.submit_subtask(task.id, SubtaskRequest(title="A", instructions="i"))
    assert a.status is SubtaskStatus.succeeded

    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.succeeded
    assert b.depends_on == [a.id]


def test_depends_on_unknown_id_is_invalid_request(task_svc):
    svc, task = task_svc
    with pytest.raises(InvalidRequest):
        svc.submit_subtask(task.id, SubtaskRequest(
            title="B", instructions="j", depends_on=["subtask_doesnotexist"]))
    # Rejected before anything was written.
    assert svc.get_task(task.id).subtasks == []


def test_dependency_cycle_is_invalid_request(task_svc):
    svc, task = task_svc
    a = svc.submit_subtask(task.id, SubtaskRequest(title="A", instructions="i"))
    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))

    # Manufacture a cycle directly in storage. This cannot happen through
    # `submit_subtask` alone — dependency ids are server-minted at submission
    # time, so a caller can only ever name something that already exists, and
    # a graph built purely from "point at something that already exists"
    # edges is necessarily a DAG. A cycle could still reach storage via a
    # future batch/plan API or a direct edit, so the guard is real; this is
    # how a test forces one into existence to prove the guard fires.
    svc.storage.update_subtask(a.id, depends_on=[b.id])

    with pytest.raises(InvalidRequest):
        svc.submit_subtask(task.id, SubtaskRequest(
            title="C", instructions="k", depends_on=[a.id]))


def test_depends_on_omitted_is_unchanged_from_before(task_svc):
    """Regression guard: default (empty) `depends_on` must be byte-identical
    to pre-dependency behavior — dispatched immediately, no blocked status,
    no blocked_on metadata."""
    svc, task = task_svc
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    assert subtask.status is SubtaskStatus.succeeded
    assert subtask.depends_on == []
    assert "blocked_on" not in subtask.metadata


def _exploding_cloud_worker():
    """A cloud worker (so it parks for approval, letting a dependent be
    submitted `blocked` against it) that crashes when finally run."""
    def boom(prompt):
        raise RuntimeError("harness segfaulted")

    return RawModelWorker(
        "cloud_boom", boom, model="deepseek-v3", location=WorkerLocation.cloud,
        privacy_tiers=[PrivacyTier.public, PrivacyTier.public_redacted],
        capability_tags=["generate"], cost_per_1k_tokens_usd=1.0,
    )


def test_terminal_dependency_failure_cascades_blocked_dependent_to_failed(tmp_path):
    """A dependency that terminally fails can never be `succeeded`, so a
    dependent still `blocked` on it must be failed — not stranded forever."""
    svc = make_service(
        tmp_path, [_exploding_cloud_worker(), EchoWorker()],
        policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    a = svc.submit_subtask(task.id, SubtaskRequest(
        title="A", instructions="i", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"]))
    assert a.status is SubtaskStatus.needs_approval  # cloud worker parks

    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.blocked

    # Approving A lets it run — and its worker crashes, so A terminally fails
    # via `_record_result`. B must be cascaded, not left `blocked`.
    failed_a = svc.approve_subtask(a.id, "matthew", max_cost_usd=3.0)
    assert failed_a.status is SubtaskStatus.failed

    b_after = svc.get_subtask(b.id).subtask
    assert b_after.status is SubtaskStatus.failed
    assert b_after.metadata.get("dependency_failed") == a.id
    assert "blocked_on" not in b_after.metadata
    assert svc.storage.list_runs(b.id) == []  # never ran — no work was done


class _DenyingGovernor:
    """In-process governor that denies one tool, driving the denied subtask
    through `_block_subtask_on_governor` (queued -> failed)."""

    def __init__(self, deny):
        self.mode = "required"
        self._deny = deny

    def authorize(self, principal, source_id, name, context=None):
        from agentconnect.core.toolconnect_client import ToolDecision
        allowed = name not in self._deny
        return ToolDecision(
            allowed=allowed,
            reason="allowed" if allowed else f"policy forbids {name}",
            decision_id=f"dec-{name}", default_deny=False,
            determining_policies=(f"p-{name}",), contract_version="1.0",
        )

    def record(self, decision_id, outcome, detail=None, *, grant_id=None):
        return {"recorded": True}

    def health(self):
        return {"status": "ok"}


def test_governor_denied_dependency_cascades_blocked_dependent_to_failed(tmp_path):
    """The other terminal-failure sink: a dependency refused at the governor
    chokepoint (`_block_subtask_on_governor`) must cascade to a dependent that
    is already `blocked` on it, not strand it."""
    svc = make_service(
        tmp_path, [cloud_worker(tags=["generate"])],
        policy=RoutePolicy(max_cost_usd=10.0))
    svc.bind_tool_governor(_DenyingGovernor(deny={"generate"}))
    task = svc.create_task(CreateTaskRequest(title="t"))

    # A is a cloud subtask, so it parks for approval before the governor is
    # consulted — long enough to submit B `blocked` against it.
    a = svc.submit_subtask(task.id, SubtaskRequest(
        title="A", instructions="i", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"]))
    assert a.status is SubtaskStatus.needs_approval

    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.blocked

    # Approval releases A to run; the governor denies its tool, failing A via
    # the chokepoint path. B must be cascaded, never left `blocked`.
    denied_a = svc.approve_subtask(a.id, "matthew", max_cost_usd=3.0)
    assert denied_a.status is SubtaskStatus.failed

    b_after = svc.get_subtask(b.id).subtask
    assert b_after.status is SubtaskStatus.failed
    assert b_after.metadata.get("dependency_failed") == a.id
    assert svc.storage.list_runs(b.id) == []  # never ran


def test_blocked_subtask_blocks_completion_as_succeeded(tmp_path):
    """A stranded `blocked` subtask is unresolved work: the parent task must
    not audit-clean and complete as `succeeded` over the top of it."""
    svc = make_service(tmp_path, [ExplodingWorker()])
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    svc.launch_session("claude", task_id=task.id, claim=True)

    a = svc.submit_subtask(task.id, SubtaskRequest(title="A", instructions="i"))
    assert a.status is SubtaskStatus.failed  # exploding worker

    # B is submitted *after* A already failed, so the failure cascade never saw
    # it (it did not exist yet) and it is left stranded in `blocked`.
    b = svc.submit_subtask(task.id, SubtaskRequest(
        title="B", instructions="j", depends_on=[a.id]))
    assert b.status is SubtaskStatus.blocked

    report = svc.audit_task(task.id)
    resolved = next(c for c in report.checks if c.name == "subtasks_resolved")
    assert resolved.passed is False
    assert b.id in resolved.detail

    with pytest.raises(PolicyViolation):
        svc.complete_task(task.id, "matthew")
    assert svc.get_task(task.id).task.status is not TaskStatus.succeeded

    # An operator with a reason can still force past it.
    forced = svc.complete_task(task.id, "matthew", force=True)
    assert forced["status"] == "succeeded"


# ------------------------------------------------------------------ subtask ops
def test_cancel_subtask_then_cancel_again_is_idempotent_noop(tmp_path):
    """Converged semantic (docs/CONSISTENCY_REVIEW.md adjudication): a second
    `cancel_subtask` on an already-cancelled subtask is an idempotent no-op,
    not a `Conflict` — double-cancel is an expected retry outcome (Temporal
    retries, `cancel_agent` already had to swallow this exact `Conflict`), and
    the state is reported either way, so nothing is lost by not raising. This
    test used to pin the raise; the goal's cancel-semantics directive
    supersedes it. Exactly one `subtask.cancelled` transition-audit row must
    still exist — the second call must not silently re-run the cascade.
    """
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=10.0))
    task = svc.create_task(CreateTaskRequest(title="t"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    svc.cancel_subtask(subtask.id)
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.cancelled
    svc.cancel_subtask(subtask.id)  # no raise
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.cancelled
    applied_audits = [
        e for e in svc.list_events(task.id)
        if e.kind == "transition" and e.payload.get("entity_id") == subtask.id
        and e.payload.get("dst") == "cancelled" and e.payload.get("outcome") == "applied"
    ]
    assert len(applied_audits) == 1


def test_registry_route_is_pure_and_does_not_touch_the_ledger():
    registry = WorkerRegistry([EchoWorker()])
    from agentconnect.core.models import Subtask

    subtask = Subtask(id="subtask_x", parent_task_id="task_x", title="t", instructions="i")
    first = route(subtask, registry)
    second = route(subtask, registry)
    assert first.model_dump() == second.model_dump()


def test_compute_placed_reports_where_the_work_actually_went(tmp_path):
    """`compute.placed` must name the SELECTED worker's location.

    Regression: the emit read `explanation.selected_location`, which did not
    exist on the model, then fell back to `approval_location` (only ever set for
    a *blocked* candidate, never on the selected path) and finally to the literal
    "local". Every route — cloud and rented included — was therefore reported as
    local, and any consumer scoring placements off this event was reading a
    constant.
    """
    svc = make_service(tmp_path, [cloud_worker()], policy=RoutePolicy(max_cost_usd=100.0))
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="me"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="s", instructions="summarize the changelog",
        privacy_tier=PrivacyTier.public, required_capabilities=["generate"],
    ))
    svc.approve_subtask(sub.id, approved_by="me", max_cost_usd=50.0)

    placed = [e for e in svc.storage.list_bus_events(since=0, limit=500)
              if e["type"] == "compute.placed"]
    assert placed, "a routed subtask must emit compute.placed"
    assert placed[-1]["payload"]["location"] == "cloud", (
        "a cloud worker's placement reported as "
        f"{placed[-1]['payload']['location']!r}"
    )
    assert svc.explain_route(sub.id).selected_location == "cloud"


def test_a_local_route_still_reports_local(tmp_path):
    svc = make_service(tmp_path, [local_model_worker()])
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="me"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="s", instructions="inspect the tree",
        privacy_tier=PrivacyTier.repo_sensitive, required_capabilities=["inspect"],
    ))
    placed = [e for e in svc.storage.list_bus_events(since=0, limit=500)
              if e["type"] == "compute.placed"]
    assert placed[-1]["payload"]["location"] == "local"
    assert svc.explain_route(sub.id).selected_location == "local"
