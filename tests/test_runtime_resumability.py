"""Mid-run resumability via LangGraph's SqliteSaver checkpointer.

With ``RuntimeConfig.checkpoint_root`` set, the graph state is persisted by a durable
SQLite checkpointer and the workspace is durable (keyed by task_id), so re-dispatching
the same task_id resumes from the exact pending node — prior nodes are NOT re-run.
Offline — the "crash" is a model source that raises mid-run; the resume is a fresh
runtime pointed at the same checkpoint_root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskConstraints, TaskSubmission
from agentconnect.runtime import LangGraphAgentRuntime, RuntimeConfig
from agentconnect.runtime.agent import _safe_dirname


def _finish(summary: str) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9})


def _write(path: str, content: str) -> str:
    return json.dumps({"action": "write_file", "path": path, "content": content})


class ScriptedModelSource:
    def __init__(self, replies: list[str]):
        self.replies = replies
        self.n = 0

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


class CrashAfter(ScriptedModelSource):
    """Serves scripted replies, then raises on the Nth generate() call — a mid-run
    process death, but deterministic and offline."""

    def __init__(self, replies: list[str], crash_on_call: int):
        super().__init__(replies)
        self._crash_on = crash_on_call

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        if self.n + 1 >= self._crash_on:
            raise RuntimeError("simulated worker crash")
        return super().generate(req)


def _sub() -> TaskSubmission:
    return TaskSubmission(
        task="Record a note then finish.",
        agent_type="scout",
        constraints=TaskConstraints(privacy_class="public"),
    )


def _runtime(source, ckpt_root: str) -> LangGraphAgentRuntime:
    return LangGraphAgentRuntime(source, RuntimeConfig(checkpoint_root=ckpt_root, max_steps=8))


# --------------------------------------------------------------------------- #
def test_resumable_run_completes_and_cleans_up(tmp_path):
    source = ScriptedModelSource([_write("notes.txt", "hello"), _finish("recorded the note")])
    result = _runtime(source, str(tmp_path)).run(_sub(), task_id="job-1")

    assert result.status == "completed"
    assert "notes.txt" in result.changed_artifacts
    # A completed run leaves nothing behind — the whole durable dir is removed.
    assert not (tmp_path / "job-1").exists()


def test_crash_then_resume_continues_from_pending_node(tmp_path):
    # Run 1: write a file (step 1), then crash before finishing (step 2 = act call #2).
    crashing = CrashAfter([_write("notes.txt", "durable data"), _finish("unreached")], crash_on_call=2)
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        _runtime(crashing, str(tmp_path)).run(_sub(), task_id="job-2")

    base = tmp_path / "job-2"
    wrote = base / "workspace" / "notes.txt"
    # The crash left the durable dir intact: checkpoint DB + the pre-crash file survive.
    assert base.exists() and (base / "checkpoint.sqlite").exists()
    assert wrote.exists() and wrote.read_text() == "durable data"

    # Run 2: a fresh runtime + non-crashing source resumes the SAME task_id from the
    # pending node. The crashed-past write step must NOT replay.
    resumed_source = ScriptedModelSource([_finish("finished after resume")])
    result = _runtime(resumed_source, str(tmp_path)).run(_sub(), task_id="job-2")

    assert result.status == "completed"
    assert result.summary == "finished after resume"
    # The strengthened guarantee: resume consumed exactly one act (the finish) — the
    # pre-crash act/tool were replayed from the checkpoint, not re-executed.
    assert resumed_source.n == 1
    # Pre-crash file change is carried through into the final result.
    assert "notes.txt" in result.changed_artifacts
    # Completed -> durable dir cleaned up.
    assert not base.exists()


def test_non_resumable_leaves_no_checkpoint(tmp_path):
    # checkpoint_root empty -> ephemeral, today's behavior: no durable dir created.
    source = ScriptedModelSource([_finish("one-shot")])
    rt = LangGraphAgentRuntime(source, RuntimeConfig(max_steps=8))  # no checkpoint_root
    result = rt.run(_sub(), task_id="job-3")
    assert result.status == "completed"
    assert list(tmp_path.iterdir()) == []  # nothing written under the sandbox


def test_safe_dirname_fences_separators():
    assert _safe_dirname("job-1") == "job-1"
    assert "/" not in _safe_dirname("../../etc/passwd")
    assert _safe_dirname("") == "task"
    assert _safe_dirname("a b/c") == "a_b_c"
