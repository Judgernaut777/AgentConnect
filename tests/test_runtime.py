"""Agent runtime: LangGraph act/tool loop, action protocol, workspace confinement.

All tests run offline. A ScriptedModelSource plays the model's side of the
action protocol; the last test drives the loop through the model-manager stub
backend to prove the ModelSource seam matches the rest of the system.
"""

from __future__ import annotations

import json

import pytest

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.runtime import (
    LangGraphAgentRuntime,
    RuntimeConfig,
    Workspace,
    WorkspaceError,
    parse_action,
)


class ScriptedModelSource:
    """Replays a fixed sequence of model replies; repeats the last one."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[GenerateRequest] = []

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.requests.append(req)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


def _finish(summary: str, **kw) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9, **kw})


# ------------------------------------------------------------ action parsing
def test_parse_action_valid_json():
    a = parse_action('{"action": "read_file", "path": "a.py"}')
    assert a.kind == "read_file" and a.args["path"] == "a.py"


def test_parse_action_tolerates_fences_and_prose():
    a = parse_action('Sure, here you go:\n```json\n{"action": "list_dir", "path": "."}\n```')
    assert a.kind == "list_dir"


def test_parse_action_prose_becomes_freeform_finish():
    a = parse_action("The answer is 42.")
    assert a.kind == "finish" and a.freeform and a.args["summary"] == "The answer is 42."


def test_parse_action_unknown_or_incomplete_is_invalid():
    assert parse_action('{"action": "rm_rf", "path": "/"}').kind == "invalid"
    assert parse_action('{"action": "write_file", "path": "a.py"}').kind == "invalid"


# ------------------------------------------------------- workspace confinement
def test_workspace_rejects_escaping_paths(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.resolve("../outside.txt")
    with pytest.raises(WorkspaceError):
        ws.resolve("a/../../outside.txt")
    assert ws.resolve("sub/inside.txt").is_relative_to(ws.root)


# ------------------------------------------------------------------ the loop
def _runtime(replies: list[str], tmp_path, **cfg) -> tuple[LangGraphAgentRuntime, ScriptedModelSource]:
    source = ScriptedModelSource(replies)
    config = RuntimeConfig(workspace_root=str(tmp_path), **cfg)
    return LangGraphAgentRuntime(source, config), source


def test_write_then_finish(tmp_path):
    rt, source = _runtime(
        [
            json.dumps({"action": "write_file", "path": "hello.txt", "content": "hi\n"}),
            _finish("Wrote hello.txt", risks=["none"], recommended_next_action="review it"),
        ],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="create hello.txt"), task_id="t1")
    assert result.status == "completed"
    assert result.summary == "Wrote hello.txt"
    assert result.confidence == 0.9
    assert result.changed_artifacts == ["hello.txt"]
    assert "none" in result.risks
    assert result.recommended_next_action == "review it"
    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    # act was called once per scripted reply
    assert len(source.requests) == 2


def test_read_feeds_observation_back_to_model(tmp_path):
    (tmp_path / "config.ini").write_text("mode=fast\n")
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "config.ini"}), _finish("mode is fast")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="what mode?"), task_id="t2")
    assert result.status == "completed"
    assert "read_file:config.ini" in result.evidence_refs
    # The second model call saw the file content as an observation.
    second_call = source.requests[1].messages
    assert any("mode=fast" in m["content"] for m in second_call if m["role"] == "user")


def test_shell_runs_in_workspace(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "shell", "command": "echo marker42"}), _finish("ran it")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="run echo"), task_id="t3")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "marker42" in obs and "exit_code=0" in obs


def test_shell_disabled_by_policy(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "shell", "command": "echo nope"}), _finish("ok")],
        tmp_path,
        allow_shell=False,
    )
    rt.run(TaskSubmission(task="try shell"), task_id="t4")
    obs = source.requests[1].messages[-1]["content"]
    assert "disabled" in obs


def test_escaping_path_becomes_error_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "../../etc/hostname"}), _finish("done")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="escape"), task_id="t5")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "ERROR" in obs and "escapes the workspace" in obs


def test_invalid_action_gets_retry_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "teleport", "to": "prod"}), _finish("recovered")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="be weird"), task_id="t6")
    assert result.status == "completed" and result.summary == "recovered"
    obs = source.requests[1].messages[-1]["content"]
    assert "unknown action" in obs


def test_max_steps_cuts_off_looping_model(tmp_path):
    rt, _ = _runtime(
        [json.dumps({"action": "list_dir", "path": "."})],  # loops forever
        tmp_path,
        max_steps=3,
    )
    result = rt.run(TaskSubmission(task="loop"), task_id="t7")
    assert result.status == "incomplete"
    assert "max_steps_reached_before_finish" in result.risks


def test_prose_reply_is_final_answer(tmp_path):
    rt, _ = _runtime(["Everything looks fine, no changes needed."], tmp_path)
    result = rt.run(TaskSubmission(task="assess"), task_id="t8")
    assert result.status == "completed"
    assert result.summary == "Everything looks fine, no changes needed."


def test_long_observation_truncated(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 10_000)
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "big.txt"}), _finish("read it")],
        tmp_path,
        observation_max_chars=500,
    )
    rt.run(TaskSubmission(task="read big"), task_id="t9")
    obs = source.requests[1].messages[-1]["content"]
    assert "[observation truncated]" in obs and len(obs) < 700


# -------------------------------------------- integration with the stub backend
def test_runs_against_model_manager_stub_backend(tmp_path):
    """The ModelSource seam is the ModelBackend interface: the loop must run
    against the stub backend unchanged. Stub output is prose, so the run
    resolves as a free-form finish."""
    from agentconnect.model_manager.backends import StubBackend

    backend = StubBackend()
    backend.load("qwen3.6-35b-a3b")
    rt = LangGraphAgentRuntime(backend, RuntimeConfig(workspace_root=str(tmp_path)))
    result = rt.run(TaskSubmission(task="summarize the repo"), task_id="t10")
    assert result.status in ("completed", "incomplete")
    assert result.summary  # stub always says something
