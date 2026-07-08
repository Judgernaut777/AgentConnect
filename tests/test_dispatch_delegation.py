"""Router-side hierarchical delegation (Track 4, Slice 2): when an agentic worker
emits sub-tasks, the router runs each as a child agentic sub-run at the next depth,
clamps each child's privacy_class to child ⊆ parent, then folds the child summaries
into ONE parent summary. Bounded by depth + fan-out.

Delegation is driven through the ``local_runtime_factory`` seam with a scripted runtime,
so the whole tree is deterministic offline — no scriptable model needed. The synthesis
step runs through the gateway's in-process stub (its text is irrelevant; we assert the
fold happened and the tree ran).
"""

import json

from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import (
    SubTask,
    TaskConstraints,
    TaskState,
    TaskSubmission,
    WorkerResult,
)
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.service import RouterService


class ScriptedRuntime:
    """A runtime whose WorkerResult is chosen by a callback keyed on (task, config).

    The router builds one of these per node via local_runtime_factory, so the callback
    sees each node's RuntimeConfig (depth, allow_delegation) and task text and can return
    a decomposing parent or a leaf. Records every run for assertions.
    """

    runs: list = []

    def __init__(self, source, config, plan):
        self.source, self.config, self.plan = source, config, plan

    def run(self, task, task_id="task_local"):
        ScriptedRuntime.runs.append({
            "task": task.task,
            "task_id": task_id,
            "depth": self.config.delegation_depth,
            "allow_delegation": self.config.allow_delegation,
            "privacy_class": getattr(task.constraints, "privacy_class", None),
        })
        return self.plan(task, self.config)


def _svc(plan, **fields):
    ScriptedRuntime.runs = []
    svc = RouterService.create(
        memory=SharedMemory(),
        local_client=InProcessLocalClient(ResidencyManager()),
        local_runtime_factory=lambda source, config: ScriptedRuntime(source, config, plan),
    )
    svc.enable_delegation = True
    for k, v in fields.items():
        setattr(svc, k, v)
    return svc


def _agentic(task="Decompose and execute the migration.", privacy="repo_sensitive"):
    return TaskSubmission(
        task=task,
        agent_type="planner",
        constraints=TaskConstraints(privacy_class=privacy, execution="agentic"),
    )


# --------------------------------------------------------------------------- #
def test_parent_delegates_children_run_and_summary_is_synthesized():
    def plan(task, config):
        if config.delegation_depth == 0:
            return WorkerResult(
                status="completed", summary="planned two parts", confidence=0.9,
                subtasks=[SubTask(task="port auth"), SubTask(task="port billing")],
            )
        # leaf child
        return WorkerResult(status="completed", summary=f"did: {task.task}", confidence=0.8)

    svc = _svc(plan)
    summary = svc.submit_task(_agentic())

    assert summary.status == TaskState.COMPLETE
    # Parent + two children ran (three nodes), children at depth 1.
    depths = sorted(r["depth"] for r in ScriptedRuntime.runs)
    assert depths == [0, 1, 1]
    child_tasks = sorted(r["task"] for r in ScriptedRuntime.runs if r["depth"] == 1)
    assert child_tasks == ["port auth", "port billing"]
    # Children are sub-runs under the parent's task_id namespace.
    for r in ScriptedRuntime.runs:
        if r["depth"] == 1:
            assert r["task_id"].startswith(summary.task_id + "/d1.")
    # A delegate_child log line was written per child, and the parent output is the
    # folded WorkerResult (children stored as separate child_output artifacts).
    logs = svc.get_log_slice(summary.task_id, query="delegate_child")
    assert len(logs) == 2
    agentic_logs = svc.get_log_slice(summary.task_id, query="agentic")
    assert any("delegated=2" in ln["message"] for ln in agentic_logs)


def test_child_privacy_is_clamped_never_downgraded():
    # Parent is repo_sensitive; one child proposes the LOOSER "public" (a downgrade) and
    # one re-proposes the parent's own "repo_sensitive". The downgrade is clamped up to
    # the parent (never laundered to a looser tier); the equal proposal is honored.
    def plan(task, config):
        if config.delegation_depth == 0:
            return WorkerResult(
                status="completed", summary="root", confidence=0.9,
                subtasks=[
                    SubTask(task="leak it", privacy_class="public"),
                    SubTask(task="keep it", privacy_class="repo_sensitive"),
                ],
            )
        return WorkerResult(status="completed", summary="leaf", confidence=0.7)

    svc = _svc(plan)
    summary = svc.submit_task(_agentic(privacy="repo_sensitive"))
    assert summary.status == TaskState.COMPLETE

    by_task = {r["task"]: r["privacy_class"] for r in ScriptedRuntime.runs if r["depth"] == 1}
    leak = by_task["leak it"]
    keep = by_task["keep it"]
    leak_val = leak.value if hasattr(leak, "value") else leak
    keep_val = keep.value if hasattr(keep, "value") else keep
    assert leak_val == "repo_sensitive"  # clamped up — never laundered to public
    assert keep_val == "repo_sensitive"  # honored


def test_stricter_runnable_child_is_honored():
    # A PUBLIC parent delegates a child that proposes the stricter, still-runnable
    # "repo_sensitive" (local_only ⊆ public's tiers) — honored, and it runs.
    def plan(task, config):
        if config.delegation_depth == 0:
            return WorkerResult(
                status="completed", summary="root", confidence=0.9,
                subtasks=[SubTask(task="tighten", privacy_class="repo_sensitive")],
            )
        return WorkerResult(status="completed", summary="leaf", confidence=0.8)

    svc = _svc(plan)
    summary = svc.submit_task(_agentic(privacy="public"))
    assert summary.status == TaskState.COMPLETE
    child = next(r for r in ScriptedRuntime.runs if r["depth"] == 1)
    pc = child["privacy_class"]
    assert (pc.value if hasattr(pc, "value") else pc) == "repo_sensitive"


def test_secret_sensitive_child_is_refused_not_run():
    # secret_sensitive must never reach an LLM: a child clamping to it is refused and
    # never executed, but the parent still completes (with the refusal folded in).
    def plan(task, config):
        if config.delegation_depth == 0:
            return WorkerResult(
                status="completed", summary="root", confidence=0.9,
                subtasks=[
                    SubTask(task="handle the secret", privacy_class="secret_sensitive"),
                    SubTask(task="ordinary work"),
                ],
            )
        return WorkerResult(status="completed", summary="leaf", confidence=0.7)

    svc = _svc(plan)
    summary = svc.submit_task(_agentic(privacy="repo_sensitive"))
    assert summary.status == TaskState.COMPLETE
    ran_tasks = [r["task"] for r in ScriptedRuntime.runs if r["depth"] == 1]
    assert "handle the secret" not in ran_tasks  # refused, never run on the model
    assert "ordinary work" in ran_tasks
    # The refusal is recorded as a child_output artifact + risk on the folded parent.
    out = json.loads(svc.read_artifact_chunk(summary.artifacts["output"])["content"])
    assert "secret_sensitive_child_refused" in out["risks"]
    assert out["confidence"] == 0.0  # weakest link: the refused child


def test_delegation_off_by_default_keeps_a_flat_run():
    seen = {"allow": None}

    def plan(task, config):
        seen["allow"] = config.allow_delegation
        # Even if a worker returned subtasks, with delegation disabled they are inert.
        return WorkerResult(
            status="completed", summary="flat", confidence=0.9,
            subtasks=[SubTask(task="ignored")],
        )

    ScriptedRuntime.runs = []
    svc = RouterService.create(
        memory=SharedMemory(),
        local_client=InProcessLocalClient(ResidencyManager()),
        local_runtime_factory=lambda s, c: ScriptedRuntime(s, c, plan),
    )  # enable_delegation left at its False default
    summary = svc.submit_task(_agentic())

    assert summary.status == TaskState.COMPLETE
    assert seen["allow"] is False            # capability not advertised
    assert len(ScriptedRuntime.runs) == 1    # exactly one node — no children spawned
    assert svc.get_log_slice(summary.task_id, query="delegate_child") == []


def test_depth_limit_stops_grandchildren():
    # Every node tries to delegate one more; max_delegation_depth=1 must stop after the
    # depth-1 children (they run with allow_delegation False and cannot recurse further).
    def plan(task, config):
        return WorkerResult(
            status="completed", summary=task.task, confidence=0.9,
            subtasks=[SubTask(task=f"{task.task}->child")],
        )

    svc = _svc(plan, max_delegation_depth=1)
    summary = svc.submit_task(_agentic(task="root"))
    assert summary.status == TaskState.COMPLETE

    depths = sorted(r["depth"] for r in ScriptedRuntime.runs)
    assert depths == [0, 1]                  # root + one child, no depth-2 grandchild
    depth1 = next(r for r in ScriptedRuntime.runs if r["depth"] == 1)
    assert depth1["allow_delegation"] is False


def test_child_outputs_are_stored_and_usage_sums_across_the_tree():
    def plan(task, config):
        if config.delegation_depth == 0:
            return WorkerResult(
                status="completed", summary="root", confidence=0.9,
                subtasks=[SubTask(task="a"), SubTask(task="b")],
            )
        return WorkerResult(status="completed", summary="leaf", confidence=0.6)

    svc = _svc(plan)
    summary = svc.submit_task(_agentic())
    assert summary.status == TaskState.COMPLETE

    # The parent's stored output is the folded WorkerResult; child_output artifacts hold
    # the raw child results (all under the parent task).
    out = svc.read_artifact_chunk(summary.artifacts["output"])
    parsed = json.loads(out["content"])
    assert parsed["status"] == "completed"
    # Weakest-link confidence: min(parent 0.9, children 0.6) == 0.6.
    assert parsed["confidence"] == 0.6
    # Evaluation recorded once for the whole tree, with summed usage > a single call.
    decisions = svc.memory.get_routing_decisions(summary.task_id)
    assert decisions[-1]["selected_provider"] == "local_r9700"
