"""Conformance: the six LocalComputeProvider HTTP routes, post CA-1/CA-3.

ComputeConnect (docs/CONTRACT.md there; docs/COMPUTECONNECT_CONTRACT.md here) conforms
to the surface `HttpLocalComputeProvider` speaks:

    GET  /health          GET  /models        GET  /models/loaded
    POST /route/estimate  POST /generate      POST /runs/{run_id}/cancel

with two ratified amendments:

* **CA-1** — `POST /generate` carries `privacy_tier`, so execution can re-verify the
  privacy decision made at estimate time. `None`/absent means "assume the most
  restrictive tier"; sending it can therefore only tighten, never loosen.
* **CA-3** (the run_id half of the original CA-2) — `/generate` responses carry a `run_id`, making
  `POST /runs/{run_id}/cancel` usable. Older engines that omit it are tolerated.

These run against a real localhost HTTP stub — NOT an injected transport — so the
lazy-httpx production path (`_call`, `raise_for_status`, JSON decode) is the code
under test. A real ComputeConnect will be integration-tested against the same shapes.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentconnect.core.local_compute import (
    HttpLocalComputeProvider,
    LocalEstimateRequest,
    LocalRunRequest,
)

pytest.importorskip("httpx", reason="conformance exercises the real httpx path")


MODELS = [
    {"id": "qwen3-30b", "runtime": "llama.cpp", "capabilities": ["generate", "code"],
     "context_tokens": 32768, "loaded": True},
    {"id": "gemma-9b", "runtime": "llama.cpp", "capabilities": ["generate"],
     "context_tokens": 8192, "loaded": False},
]


class _StubEngine(BaseHTTPRequestHandler):
    """A compliant post-amendment engine. Records every request it serves."""

    requests: list[tuple[str, str, dict | None]] = []  # (method, path, body)
    omit_run_id = False  # flip to emulate a pre-CA-3 engine

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        type(self).requests.append(("GET", self.path, None))
        if self.path == "/health":
            self._reply(200, {"status": "ok", "engine": "stub", "version": "1"})
        elif self.path == "/models":
            self._reply(200, {"models": MODELS})
        elif self.path == "/models/loaded":
            self._reply(200, {"models": [m for m in MODELS if m["loaded"]]})
        else:
            self._reply(404, {"error": "no such route"})

    def do_POST(self):  # noqa: N802
        body = self._body()
        type(self).requests.append(("POST", self.path, body))
        if self.path == "/route/estimate":
            self._reply(200, {
                "eligible": True, "selected_model": "qwen3-30b", "runtime": "llama.cpp",
                "loaded": True, "estimated_queue_seconds": 0.5,
                "estimated_tokens_per_second": 25.0, "estimated_quality": 0.7,
                "reason": {"policy": "fits"},
            })
        elif self.path == "/generate":
            reply = {
                "status": "succeeded", "output": "stub output",
                "model": "qwen3-30b", "runtime": "llama.cpp",
                "metrics": {"tokens_out": 2}, "warnings": [],
            }
            if not type(self).omit_run_id:
                reply["run_id"] = "run_stub_001"
            self._reply(200, reply)
        elif self.path.startswith("/runs/") and self.path.endswith("/cancel"):
            self._reply(200, {"status": "cancelling"})
        else:
            self._reply(404, {"error": "no such route"})

    def log_message(self, *args):  # quiet
        return


@pytest.fixture()
def engine():
    _StubEngine.requests = []
    _StubEngine.omit_run_id = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubEngine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def provider(engine):
    return HttpLocalComputeProvider(engine, timeout=10.0)


# ---------------------------------------------------------------- the six routes
def test_health_route(provider):
    assert provider.health()["status"] == "ok"
    assert _StubEngine.requests == [("GET", "/health", None)]


def test_models_route(provider):
    models = provider.inventory()
    assert [m.id for m in models] == ["qwen3-30b", "gemma-9b"]
    assert models[0].context_tokens == 32768 and models[0].loaded
    assert _StubEngine.requests == [("GET", "/models", None)]


def test_models_loaded_route(provider):
    loaded = provider.loaded()
    assert [m.id for m in loaded] == ["qwen3-30b"]
    assert _StubEngine.requests == [("GET", "/models/loaded", None)]


def test_route_estimate_carries_the_pinned_request_shape(provider):
    estimate = provider.estimate(LocalEstimateRequest(
        task_type="code", privacy_tier="repo_sensitive",
        required_capabilities=["code"], context_tokens=900, max_output_tokens=256,
    ))
    assert estimate.eligible and estimate.selected_model == "qwen3-30b"
    (method, path, body), = _StubEngine.requests
    assert (method, path) == ("POST", "/route/estimate")
    # The exact input contract ComputeConnect's CONTRACT.md pins.
    assert set(body) == {
        "task_type", "privacy_tier", "required_capabilities", "context_tokens",
        "max_output_tokens", "latency_preference", "quality_preference",
    }
    assert body["privacy_tier"] == "repo_sensitive"


def test_generate_sends_privacy_tier_ca1(provider):
    result = provider.run(LocalRunRequest(
        model="qwen3-30b", task_type="code", prompt="write a haiku",
        privacy_tier="repo_sensitive",
    ))
    assert result.status == "succeeded" and result.output == "stub output"
    (_, path, body), = _StubEngine.requests
    assert path == "/generate"
    assert body["privacy_tier"] == "repo_sensitive"


def test_generate_without_tier_sends_null_never_a_guess(provider):
    """An unset tier crosses the wire as null: the engine must assume the most
    restrictive tier, and this client never invents a looser one."""
    provider.run(LocalRunRequest(model=None, task_type="general", prompt="p"))
    (_, _, body), = _StubEngine.requests
    assert "privacy_tier" in body and body["privacy_tier"] is None


def test_generate_surfaces_run_id_ca2(provider):
    result = provider.run(LocalRunRequest(model=None, task_type="general", prompt="p",
                                          privacy_tier="public"))
    assert result.run_id == "run_stub_001"


def test_generate_tolerates_missing_run_id_pre_ca2_engine(provider):
    _StubEngine.omit_run_id = True
    result = provider.run(LocalRunRequest(model=None, task_type="general", prompt="p"))
    assert result.status == "succeeded"
    assert result.run_id is None  # tolerated, never required


def test_cancel_route_uses_the_returned_run_id(provider):
    run_id = provider.run(LocalRunRequest(model=None, task_type="general",
                                          prompt="p")).run_id
    provider.cancel(run_id)
    assert _StubEngine.requests[-1][:2] == ("POST", f"/runs/{run_id}/cancel")


def test_cancel_is_best_effort_never_raises(provider):
    # The stub 404s unknown routes; an engine that lost the run must not crash us.
    provider.cancel("run_the_engine_forgot")  # must not raise
    assert _StubEngine.requests[-1][:2] == ("POST", "/runs/run_the_engine_forgot/cancel")
