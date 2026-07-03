"""Worker-over-HTTP transport: serve an ``AgentRuntime``, and call one remotely.

The app factory configures **no TLS** — transport authentication is mutual TLS
at the server launcher (uvicorn ``ssl_cert_reqs=CERT_REQUIRED`` +
``ssl_ca_certs``, see ``agentconnect.model_manager.tls.build_ssl_kwargs``).
Never bind a non-loopback interface without it; there is no shared secret on
this wire ever — identity is the certificate.

``RuntimeConfig`` is worker-side only: the wire carries ``task_id`` plus
``TaskSubmission`` and nothing else, so a router can never relax
``allow_shell``/``allow_tests``/``allow_browser`` or workspace policy remotely.
Workspace confinement, observation truncation, and the ERROR-string
conventions all run server-side inside the runtime exactly as they do locally.

Capacity is a lock-guarded counter because FastAPI sync endpoints run in a
threadpool. fastapi and httpx are imported lazily (the ``remote`` extra), so
importing this module — and re-exporting from the package — needs neither.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel

from agentconnect.common.schemas import CanAcceptResponse, TaskSubmission, WorkerResult

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

    from agentconnect.common.config import TlsClientConfig

    from .agent import AgentRuntime


@dataclass(frozen=True)
class RuntimeEndpoint:
    kind: str  # local | http
    address: str


class RunTaskRequest(BaseModel):
    """Wire contract for POST /run. Defined here rather than in core schemas:
    both ends of this wire live in this module (precedent: approval_web's
    BudgetBody)."""

    task_id: str
    submission: TaskSubmission


def create_worker_app(
    runtime: "AgentRuntime",
    *,
    worker_id: str = "worker_local",
    max_concurrent_tasks: int = 1,
    allowed_clients: Optional[set[str]] = None,
) -> "FastAPI":
    """FastAPI app serving ``runtime``: ``POST /run`` and ``GET /can_accept``.

    ``max_concurrent_tasks`` defaults to 1 — ``LangGraphAgentRuntime`` is
    documented single-task; raising it requires a thread-safe model source.
    Each ``run()`` creates and cleans a fresh workspace, so one runtime
    instance serves many sequential tasks. ``allowed_clients`` mounts the same
    defense-in-depth identity allowlist as ``model_manager.app.create_app``.
    """
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title=f"AgentConnect Worker {worker_id}", version="0.1.0")
    if allowed_clients:
        from agentconnect.common.asgi_identity import ClientIdentityMiddleware

        app.add_middleware(ClientIdentityMiddleware, allowed=allowed_clients)

    lock = threading.Lock()
    running = [0]

    @app.post("/run")
    def run_task(req: RunTaskRequest) -> dict:
        with lock:
            if running[0] >= max_concurrent_tasks:
                raise HTTPException(status_code=503, detail="worker at capacity")
            running[0] += 1
        try:
            try:
                result = runtime.run(req.submission, task_id=req.task_id)
            except Exception as exc:  # noqa: BLE001 — the endpoint stays total:
                # TaskSubmission in -> WorkerResult out; a completed exchange
                # carrying a failed contract beats a 500 for the router.
                result = WorkerResult(
                    status="failed",
                    summary=f"ERROR: worker exception: {exc}"[:400],
                    confidence=0.0,
                    risks=["worker_exception"],
                )
            return result.model_dump()
        finally:
            with lock:
                running[0] -= 1

    @app.get("/can_accept")
    def can_accept() -> dict:
        with lock:
            busy = running[0] >= max_concurrent_tasks
        if busy:
            return CanAcceptResponse(can_accept=False, reason="worker at capacity").model_dump()
        return CanAcceptResponse(can_accept=True).model_dump()

    return app


class HttpAgentRuntime:
    """``AgentRuntime`` over HTTP. Authentication is the X.509 client
    certificate (``TlsClientConfig``, ``mode="mutual"``); plain HTTP is for
    ``insecure_localhost`` and injected test clients only.

    Transport/HTTP failures raise (httpx exceptions propagate), matching
    ``HttpLocalClient`` — the router's dispatch path already converts dispatch
    exceptions to FAILED.
    """

    def __init__(
        self,
        base_url: str,
        *,
        tls: Optional["TlsClientConfig"] = None,
        timeout: float = 600.0,  # runs can take max_steps * shell_timeout
        client: Optional["httpx.Client"] = None,  # test seam: starlette TestClient
    ):
        if client is not None:
            self._client = client
            return
        import httpx

        from agentconnect.common.config import client_ssl_context

        base = base_url.rstrip("/")
        ctx = client_ssl_context(tls)
        if ctx is not None:
            self._client = httpx.Client(base_url=base, verify=ctx, timeout=timeout)
        else:
            # insecure_localhost / no TLS material — plain HTTP, loopback only.
            self._client = httpx.Client(base_url=base, timeout=timeout)

    def run(self, task: TaskSubmission, task_id: str = "task_remote") -> WorkerResult:
        r = self._client.post(
            "/run", json={"task_id": task_id, "submission": task.model_dump(mode="json")}
        )
        r.raise_for_status()
        return WorkerResult.model_validate(r.json())

    def can_accept(self) -> CanAcceptResponse:
        r = self._client.get("/can_accept")
        r.raise_for_status()
        return CanAcceptResponse.model_validate(r.json())
