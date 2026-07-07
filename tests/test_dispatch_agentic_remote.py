"""Router-driven remote-worker dispatch: an agentic task is PUSHED whole to a
registered remote worker over the AgentRuntime wire, its WorkerResult folded into
the state machine, and its self-reported usage recorded — with a fail-closed
trust gate (WorkQueue.may_claim) and in-process fallback when no worker is
eligible/available. Offline via the starlette TestClient seam (no real network).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from agentconnect.common.config import RemoteWorkerConfig, TlsClientConfig, load_remote_workers
from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import (
    CanAcceptResponse,
    GenerateRequest,
    GenerateResponse,
    TaskConstraints,
    TaskState,
    TaskSubmission,
    Usage,
    WorkerResult,
)
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.service import RouterService
from agentconnect.runtime import (
    HttpAgentRuntime,
    LangGraphAgentRuntime,
    RuntimeConfig,
    create_worker_app,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #
def _finish(summary: str, **kw) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9, **kw})


class UsageModelSource:
    """One-shot finish reply carrying fixed token counts, to prove usage flows
    from the worker's model all the way into the router's records."""

    def __init__(self, in_tok: int = 7, out_tok: int = 3, model_id: str = "worker-model"):
        self.in_tok, self.out_tok, self.model_id = in_tok, out_tok, model_id

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        return GenerateResponse(
            request_id=req.request_id,
            model_id=self.model_id,
            output_text=_finish("Did the remote work"),
            input_tokens=self.in_tok,
            output_tokens=self.out_tok,
        )


class _StubRuntime:
    """An AgentRuntime whose can_accept / run behavior is fully scripted, for the
    selection-logic paths (busy, accept-then-fail) where a real loop is overkill."""

    def __init__(self, *, accept: bool = True, raise_on_run: bool = False):
        self._accept = accept
        self._raise = raise_on_run
        self.ran = False

    def can_accept(self) -> CanAcceptResponse:
        return CanAcceptResponse(can_accept=self._accept, reason="" if self._accept else "busy")

    def run(self, task, task_id="task_remote") -> WorkerResult:
        self.ran = True
        if self._raise:
            raise RuntimeError("worker dropped mid-run")
        return WorkerResult(status="completed", summary="ok", usage=Usage(input_tokens=1, output_tokens=1))


def _worker_app(source, tmp_path):
    return create_worker_app(
        LangGraphAgentRuntime(source, RuntimeConfig(workspace_root=str(tmp_path)))
    )


def _service_with_workers(workers, factory, *, local: bool = True) -> RouterService:
    """A router whose in-process path is LOCAL-capable (so fallback completes),
    with the remote registry + runtime factory injected directly (the same seam
    RouterService.create wires from config)."""
    kwargs = {"memory": SharedMemory()}
    if local:
        kwargs["local_client"] = InProcessLocalClient(ResidencyManager())
    svc = RouterService.create(**kwargs)
    svc.remote_workers = workers
    svc.remote_runtime_factory = factory
    return svc


def _agentic(privacy_class: str = "repo_sensitive") -> TaskSubmission:
    return TaskSubmission(
        task="Refactor the auth/session token refresh path in this private module.",
        agent_type="patch_worker",
        constraints=TaskConstraints(privacy_class=privacy_class, execution="agentic"),
    )


def _cfg(worker_id: str, tier: str) -> RemoteWorkerConfig:
    return RemoteWorkerConfig(worker_id=worker_id, endpoint="http://testserver", tier=tier)


# --------------------------------------------------------------------------- #
# Dispatch happy path + metering
# --------------------------------------------------------------------------- #
def test_eligible_worker_dispatches_and_records_usage(tmp_path):
    app = _worker_app(UsageModelSource(in_tok=7, out_tok=3), tmp_path)
    factory = lambda w: HttpAgentRuntime("http://testserver", client=TestClient(app))  # noqa: E731
    svc = _service_with_workers([_cfg("fleet-1", "local_only")], factory)

    summary = svc.submit_task(_agentic("repo_sensitive"))

    assert summary.status == TaskState.COMPLETE
    # The stored output is the structured WorkerResult from the remote worker.
    chunk = svc.read_artifact_chunk(summary.artifacts["output"])
    assert '"status": "completed"' in chunk["content"]
    # Proof it went remote, not in-process: the remote_dispatch log line, and the
    # worker-reported usage threaded into the record.
    remote_logs = svc.get_log_slice(summary.task_id, query="remote_dispatch")
    assert any("worker=fleet-1" in ln["message"] and "tier=local_only" in ln["message"] for ln in remote_logs)
    usage_logs = svc.get_log_slice(summary.task_id, query="agentic_remote")
    assert any("in=7 out=3" in ln["message"] for ln in usage_logs)
    # The in-process agentic loop never ran (no "steps=" log).
    assert not any("steps=" in ln["message"] for ln in svc.get_log_slice(summary.task_id, query="agentic"))


# --------------------------------------------------------------------------- #
# Fallback paths
# --------------------------------------------------------------------------- #
def test_untrusted_tier_falls_back_in_process(tmp_path):
    # An external-tier worker is NOT admitted for repo_sensitive -> skipped ->
    # the task runs in-process on the local model.
    app = _worker_app(UsageModelSource(), tmp_path)
    factory = lambda w: HttpAgentRuntime("http://testserver", client=TestClient(app))  # noqa: E731
    svc = _service_with_workers([_cfg("cloud-box", "external")], factory)

    summary = svc.submit_task(_agentic("repo_sensitive"))

    assert summary.status == TaskState.COMPLETE
    assert svc.get_log_slice(summary.task_id, query="remote_dispatch") == []
    # In-process agentic really ran (the distinctive "steps=" log).
    assert any("steps=" in ln["message"] for ln in svc.get_log_slice(summary.task_id, query="agentic"))


def test_no_worker_registered_runs_in_process():
    # Default construction: no remote workers -> today's behavior unchanged.
    svc = RouterService.create(
        memory=SharedMemory(), local_client=InProcessLocalClient(ResidencyManager())
    )
    assert svc.remote_workers == []
    summary = svc.submit_task(_agentic("repo_sensitive"))
    assert summary.status == TaskState.COMPLETE
    assert svc.get_log_slice(summary.task_id, query="remote_dispatch") == []


def test_busy_worker_falls_back_in_process():
    busy = _StubRuntime(accept=False)
    svc = _service_with_workers([_cfg("fleet-1", "local_only")], lambda w: busy)

    summary = svc.submit_task(_agentic("repo_sensitive"))

    assert summary.status == TaskState.COMPLETE
    assert busy.ran is False  # never dispatched
    assert svc.get_log_slice(summary.task_id, query="remote_dispatch") == []
    assert any("steps=" in ln["message"] for ln in svc.get_log_slice(summary.task_id, query="agentic"))


def test_unreachable_worker_falls_back_in_process():
    def _boom(w):
        raise ConnectionError("no route to worker")

    svc = _service_with_workers([_cfg("fleet-1", "local_only")], _boom)
    summary = svc.submit_task(_agentic("repo_sensitive"))
    assert summary.status == TaskState.COMPLETE
    assert svc.get_log_slice(summary.task_id, query="remote_dispatch") == []


# --------------------------------------------------------------------------- #
# Accept-then-fail is a genuine FAILED (no silent re-run)
# --------------------------------------------------------------------------- #
def test_worker_accepts_then_run_fails_is_failed():
    dropped = _StubRuntime(accept=True, raise_on_run=True)
    svc = _service_with_workers([_cfg("fleet-1", "local_only")], lambda w: dropped)

    summary = svc.submit_task(_agentic("repo_sensitive"))

    assert dropped.ran is True
    assert summary.status == TaskState.FAILED
    assert "failed" in (summary.summary or "").lower()
    assert any(
        "remote_dispatch_failed" in ln["message"]
        for ln in svc.get_log_slice(summary.task_id, query="remote_dispatch_failed")
    )
    # It did NOT silently re-run in-process (the worker may have had side effects).
    assert not any("steps=" in ln["message"] for ln in svc.get_log_slice(summary.task_id, query="agentic"))


# --------------------------------------------------------------------------- #
# Trust: repo_sensitive picks only an attested local_only worker
# --------------------------------------------------------------------------- #
def test_repo_sensitive_prefers_local_only_over_private_rented(tmp_path):
    app = _worker_app(UsageModelSource(), tmp_path)
    factory = lambda w: HttpAgentRuntime("http://testserver", client=TestClient(app))  # noqa: E731
    # Rented worker listed FIRST; it must be skipped for repo_sensitive (no
    # allow_rented widening on this path), and the local_only worker chosen.
    workers = [_cfg("rented-box", "private_rented"), _cfg("local-box", "local_only")]
    svc = _service_with_workers(workers, factory)

    summary = svc.submit_task(_agentic("repo_sensitive"))

    assert summary.status == TaskState.COMPLETE
    logs = svc.get_log_slice(summary.task_id, query="remote_dispatch")
    assert any("worker=local-box" in ln["message"] for ln in logs)
    assert not any("worker=rented-box" in ln["message"] for ln in logs)


# --------------------------------------------------------------------------- #
# Config loader
# --------------------------------------------------------------------------- #
def test_load_remote_workers_parses_tls(tmp_path, monkeypatch):
    import agentconnect.common.config as config

    (tmp_path / "remote_workers.yaml").write_text(
        "remote_workers:\n"
        "  - worker_id: box-1\n"
        "    endpoint: https://box1:8443\n"
        "    tier: local_only\n"
        "    tls:\n"
        "      mode: mutual\n"
        "      ca_cert: /etc/ca.pem\n"
        "      client_cert: /etc/cert.pem\n"
        "      client_key: /etc/key.pem\n"
        "    capabilities: [coding, review]\n"
    )
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    workers = config.load_remote_workers()
    assert len(workers) == 1
    w = workers[0]
    assert (w.worker_id, w.endpoint, w.tier) == ("box-1", "https://box1:8443", "local_only")
    assert isinstance(w.tls, TlsClientConfig) and w.tls.ca_cert == "/etc/ca.pem"
    assert w.capabilities == ("coding", "review")


def test_load_remote_workers_missing_file_is_empty(tmp_path, monkeypatch):
    import agentconnect.common.config as config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)  # empty dir, no yaml
    assert config.load_remote_workers() == []


# --------------------------------------------------------------------------- #
# Wire schema + runtime usage stamping
# --------------------------------------------------------------------------- #
def test_worker_result_usage_round_trips():
    wr = WorkerResult(status="completed", usage=Usage(input_tokens=11, output_tokens=4, model_id="m"))
    back = WorkerResult.model_validate(json.loads(wr.model_dump_json()))
    assert back.usage is not None
    assert (back.usage.input_tokens, back.usage.output_tokens, back.usage.model_id) == (11, 4, "m")
    # Backward compat: an older worker omits usage entirely.
    assert WorkerResult.model_validate({"status": "completed"}).usage is None


def test_runtime_stamps_usage_from_model_source(tmp_path):
    rt = LangGraphAgentRuntime(
        UsageModelSource(in_tok=5, out_tok=2, model_id="qwen-local"),
        RuntimeConfig(workspace_root=str(tmp_path)),
    )
    result = rt.run(TaskSubmission(task="do it"), task_id="u1")
    assert result.status == "completed"
    assert result.usage is not None
    assert (result.usage.input_tokens, result.usage.output_tokens) == (5, 2)
    assert result.usage.model_id == "qwen-local"
