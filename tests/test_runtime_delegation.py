"""Runtime delegation seam (Track 4): a planner/manager agent emits sub-tasks via
the `delegate` action, recorded on WorkerResult.subtasks for the router to run as
children. Bounded by depth + fan-out so recursion can't run away. Offline.
"""

from __future__ import annotations

import json

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskConstraints, TaskSubmission
from agentconnect.runtime import LangGraphAgentRuntime, RuntimeConfig
from agentconnect.runtime.actions import parse_action
from agentconnect.runtime.prompts import build_system_prompt


def _finish(summary: str) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9})


def _delegate(task: str, **kw) -> str:
    return json.dumps({"action": "delegate", "task": task, **kw})


class ScriptedModelSource:
    def __init__(self, replies: list[str]):
        self.replies = replies
        self.n = 0

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


def _sub() -> TaskSubmission:
    return TaskSubmission(
        task="Decompose the migration into parts.",
        agent_type="planner",
        constraints=TaskConstraints(privacy_class="public"),
    )


def _run(source, tmp_path, **cfg):
    rt = LangGraphAgentRuntime(source, RuntimeConfig(workspace_root=str(tmp_path), **cfg))
    return rt.run(_sub(), task_id="deleg1")


# --------------------------------------------------------------------------- #
def test_delegate_records_subtasks(tmp_path):
    source = ScriptedModelSource([
        _delegate("port the auth module", agent_type="patch_worker"),
        _delegate("port the billing module"),
        _finish("decomposed into two parts"),
    ])
    result = _run(source, tmp_path, allow_delegation=True)

    assert result.status == "completed"
    assert [s.task for s in result.subtasks] == ["port the auth module", "port the billing module"]
    assert result.subtasks[0].agent_type == "patch_worker"
    assert result.subtasks[1].agent_type is None
    assert any(e.startswith("delegate:") for e in result.evidence_refs)


def test_delegate_disabled_without_allow_delegation(tmp_path):
    source = ScriptedModelSource([_delegate("should not record"), _finish("done")])
    result = _run(source, tmp_path, allow_delegation=False)
    assert result.subtasks == []


def test_delegate_depth_limit_disables_further_delegation(tmp_path):
    # A worker already at the max depth cannot delegate (it's a leaf) — the action
    # reports disabled and nothing is recorded.
    source = ScriptedModelSource([_delegate("go deeper"), _finish("leaf")])
    result = _run(source, tmp_path, allow_delegation=True, delegation_depth=2, max_delegation_depth=2)
    assert result.subtasks == []


def test_delegate_fanout_cap(tmp_path):
    # max_subtasks bounds how many children one run may spawn.
    source = ScriptedModelSource([
        _delegate("a"), _delegate("b"), _delegate("c"), _finish("done"),
    ])
    result = _run(source, tmp_path, allow_delegation=True, max_subtasks=2)
    assert [s.task for s in result.subtasks] == ["a", "b"]  # third refused


def test_prompt_advertises_delegate_only_when_enabled_and_below_depth():
    on = build_system_prompt(_sub(), RuntimeConfig(allow_delegation=True, delegation_depth=0, max_delegation_depth=2))
    off = build_system_prompt(_sub(), RuntimeConfig(allow_delegation=False))
    at_leaf = build_system_prompt(_sub(), RuntimeConfig(allow_delegation=True, delegation_depth=2, max_delegation_depth=2))
    assert '"action": "delegate"' in on
    assert '"action": "delegate"' not in off
    assert '"action": "delegate"' not in at_leaf  # leaf depth: not advertised


def test_parse_action_knows_delegate():
    a = parse_action('{"action": "delegate", "task": "x"}')
    assert a.kind == "delegate" and a.args["task"] == "x"
    bad = parse_action('{"action": "delegate"}')  # missing task
    assert bad.kind == "invalid"
