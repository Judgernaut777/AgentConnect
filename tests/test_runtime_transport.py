"""Worker-over-HTTP transport: the AgentRuntime contract served and consumed
over the wire. Offline via starlette TestClient (no real network); mTLS itself
is proven by tests/test_mtls.py — here the injected-client seam stands in for
the transport layer.
"""

from __future__ import annotations

import json
import ssl
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from agentconnect.common.config import TlsClientConfig, client_ssl_context
from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.runtime import (
    HttpAgentRuntime,
    LangGraphAgentRuntime,
    RuntimeConfig,
    create_worker_app,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class ScriptedModelSource:
    """Replays a fixed sequence of model replies; repeats the last one."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[GenerateRequest] = []

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.requests.append(req)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


class BlockingModelSource:
    """Signals `started`, then holds the run open until `release` (5s cap)."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.started.set()
        self.release.wait(timeout=5)
        return GenerateResponse(
            request_id=req.request_id,
            model_id=req.model_id,
            output_text=_finish("held then done"),
        )


class BrokenModelSource:
    def generate(self, req: GenerateRequest) -> GenerateResponse:
        raise RuntimeError("model down")


def _finish(summary: str, **kw) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9, **kw})


def _runtime(source, tmp_path, **cfg) -> LangGraphAgentRuntime:
    return LangGraphAgentRuntime(source, RuntimeConfig(workspace_root=str(tmp_path), **cfg))


def test_run_round_trip(tmp_path):
    source = ScriptedModelSource(
        [
            json.dumps({"action": "write_file", "path": "hello.txt", "content": "hi\n"}),
            _finish("Wrote hello.txt"),
        ]
    )
    app = create_worker_app(_runtime(source, tmp_path))
    remote = HttpAgentRuntime("http://testserver", client=TestClient(app))
    result = remote.run(TaskSubmission(task="create hello.txt"), task_id="rt1")
    # The full contract survives serialization both ways.
    assert result.status == "completed"
    assert result.summary == "Wrote hello.txt"
    assert result.changed_artifacts == ["hello.txt"]
    assert (tmp_path / "hello.txt").read_text() == "hi\n"


def test_can_accept_idle(tmp_path):
    app = create_worker_app(_runtime(ScriptedModelSource([_finish("noop")]), tmp_path))
    client = TestClient(app)
    remote = HttpAgentRuntime("http://testserver", client=client)
    resp = remote.can_accept()
    assert resp.can_accept is True
    assert resp.reason == ""
    assert client.get("/can_accept").status_code == 200


def test_busy_worker_rejects_and_client_raises(tmp_path):
    source = BlockingModelSource()
    app = create_worker_app(_runtime(source, tmp_path))
    payload = {"task_id": "busy1", "submission": TaskSubmission(task="hold").model_dump(mode="json")}
    first: dict = {}

    def _post():
        first["resp"] = TestClient(app).post("/run", json=payload)

    t = threading.Thread(target=_post)
    t.start()
    try:
        assert source.started.wait(timeout=5)
        # While the first task holds the slot: probe says busy, /run says 503.
        remote = HttpAgentRuntime("http://testserver", client=TestClient(app))
        probe = remote.can_accept()
        assert probe.can_accept is False
        assert probe.reason == "worker at capacity"
        assert TestClient(app).post("/run", json=payload).status_code == 503
        with pytest.raises(httpx.HTTPStatusError):
            remote.run(TaskSubmission(task="rejected"), task_id="busy2")
    finally:
        source.release.set()
        t.join(timeout=10)
    assert first["resp"].status_code == 200
    assert first["resp"].json()["status"] == "completed"


def test_worker_exception_becomes_failed_result(tmp_path):
    app = create_worker_app(_runtime(BrokenModelSource(), tmp_path))
    remote = HttpAgentRuntime("http://testserver", client=TestClient(app))
    result = remote.run(TaskSubmission(task="anything"), task_id="boom")
    # The endpoint stays total: a failed contract, never a 500.
    assert result.status == "failed"
    assert result.summary.startswith("ERROR: worker exception:")
    assert "worker_exception" in result.risks


def test_malformed_submission_is_422(tmp_path):
    app = create_worker_app(_runtime(ScriptedModelSource([_finish("noop")]), tmp_path))
    assert TestClient(app).post("/run", json={"task_id": "x"}).status_code == 422


def test_worker_allowlist_middleware(tmp_path):
    app = create_worker_app(
        _runtime(ScriptedModelSource([_finish("noop")]), tmp_path),
        allowed_clients={"agentconnect-router-01"},
    )
    c = TestClient(app)
    # No identity available -> defers to the transport-layer CA check.
    assert c.get("/can_accept").status_code == 200
    assert c.get("/can_accept", headers={"X-Client-Cert-DN": "intruder"}).status_code == 403
    assert (
        c.get("/can_accept", headers={"X-Client-Cert-DN": "agentconnect-router-01"}).status_code
        == 200
    )


def test_client_ssl_context_helper():
    assert client_ssl_context(None) is None
    assert client_ssl_context(TlsClientConfig(mode="insecure_localhost")) is None
    # mode="mutual" with no cert paths: the CA/cert loads are path-gated, so
    # this builds a default-verify context without touching any files.
    assert isinstance(client_ssl_context(TlsClientConfig(mode="mutual")), ssl.SSLContext)
