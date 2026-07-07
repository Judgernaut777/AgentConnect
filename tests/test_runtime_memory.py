"""Outbound memory seam: a worker can WRITE findings (the `remember` action) but
never read memory back. Gated on allow_memory AND an injected sink; provenance
(task_id, privacy_class, agent_type) rides along for the manager's later recall.
Offline via a fake sink — no live WikiBrain.
"""

from __future__ import annotations

import json

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskConstraints, TaskSubmission
from agentconnect.runtime import LangGraphAgentRuntime, NullMemorySink, RuntimeConfig
from agentconnect.runtime.actions import parse_action
from agentconnect.runtime.prompts import build_system_prompt


def _finish(summary: str) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9})


class ScriptedModelSource:
    def __init__(self, replies: list[str]):
        self.replies = replies
        self.n = 0

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        text = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


class FakeMemorySink:
    def __init__(self, reply: str = "captured (pending review)"):
        self.calls: list[tuple[str, dict]] = []
        self.reply = reply

    def capture(self, text: str, *, provenance: dict) -> str:
        self.calls.append((text, provenance))
        return self.reply


def _sub(**kw) -> TaskSubmission:
    return TaskSubmission(
        task="Investigate the storage backend.",
        agent_type="scout",
        constraints=TaskConstraints(privacy_class="public", **kw),
    )


def _run(source, tmp_path, sink=None, allow_memory=False):
    rt = LangGraphAgentRuntime(
        source,
        RuntimeConfig(workspace_root=str(tmp_path), allow_memory=allow_memory),
        memory_sink=sink,
    )
    return rt.run(_sub(), task_id="mem1")


# --------------------------------------------------------------------------- #
def test_remember_writes_to_sink_with_provenance(tmp_path):
    sink = FakeMemorySink()
    source = ScriptedModelSource([
        json.dumps({"action": "remember", "text": "backend is SeaweedFS, not MinIO"}),
        _finish("noted the backend"),
    ])
    result = _run(source, tmp_path, sink=sink, allow_memory=True)

    assert len(sink.calls) == 1
    text, prov = sink.calls[0]
    assert text == "backend is SeaweedFS, not MinIO"
    # Provenance carries what the manager needs to judge sensitivity at recall time.
    assert prov["task_id"] == "mem1"
    assert prov["agent_type"] == "scout"
    assert prov["privacy_class"] == "public"
    # The capture is recorded as evidence on the worker result.
    assert any(e.startswith("remember:") for e in result.evidence_refs)


def test_remember_disabled_without_allow_memory(tmp_path):
    sink = FakeMemorySink()
    source = ScriptedModelSource([
        json.dumps({"action": "remember", "text": "should not be written"}),
        _finish("done"),
    ])
    _run(source, tmp_path, sink=sink, allow_memory=False)
    assert sink.calls == []  # gate closed -> sink never touched


def test_remember_disabled_without_sink(tmp_path):
    # allow_memory on but no sink injected -> the action reports disabled, no crash.
    source = ScriptedModelSource([
        json.dumps({"action": "remember", "text": "no sink here"}),
        _finish("done"),
    ])
    result = _run(source, tmp_path, sink=None, allow_memory=True)
    assert result.status == "completed"
    assert not any(e.startswith("remember:") for e in result.evidence_refs)


def test_prompt_advertises_remember_only_when_enabled():
    on = build_system_prompt(_sub(), RuntimeConfig(allow_memory=True))
    off = build_system_prompt(_sub(), RuntimeConfig(allow_memory=False))
    assert '"action": "remember"' in on
    assert '"action": "remember"' not in off


def test_parse_action_knows_remember():
    a = parse_action('{"action": "remember", "text": "x"}')
    assert a.kind == "remember" and a.args["text"] == "x"
    bad = parse_action('{"action": "remember"}')  # missing text
    assert bad.kind == "invalid"


def test_null_sink_reports_not_configured():
    out = NullMemorySink().capture("x", provenance={})
    assert out.startswith("ERROR:")
