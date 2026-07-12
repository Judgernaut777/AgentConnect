"""FastAPI adapter over `AgentConnectService` (spec §5, §11).

Every route is a translation: HTTP in, service call, model out. No route holds
policy, touches storage, or knows what a worker is.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from agentconnect.core.bootstrap import service_from_env
from agentconnect.core.errors import AgentConnectError
from agentconnect.core.service import AgentConnectService

from . import (
    routes_artifacts,
    routes_compliance,
    routes_linear,
    routes_managers,
    routes_memory,
    routes_reviews,
    routes_subtasks,
    routes_tasks,
    routes_temporal,
)
from .authz import PUBLIC_ROUTES, ROUTE_ACTIONS, WEBHOOK_ROUTES, enforce
from .deps import linear_sync_from_env, status_for


def create_app(
    service: Optional[AgentConnectService] = None,
    linear_sync: Optional[object] = None,
) -> FastAPI:
    """Every route authenticates except `GET /health`.

    `enforce` is an **app-level** dependency rather than a per-route decorator. A
    decorator protects the routes someone remembered to decorate; this protects the
    ones they did not, because a route with no declared action fails closed with a
    500 instead of serving. `unmapped_routes()` turns that into a test.
    """
    app = FastAPI(
        title="AgentConnect",
        description="Local-first task backplane for interchangeable agent managers and workers.",
        version="0.1.0",
        dependencies=[Depends(enforce)],
    )
    svc = service or service_from_env()
    app.state.service = svc
    app.state.linear_sync = linear_sync if linear_sync is not None else linear_sync_from_env(svc)

    @app.exception_handler(AgentConnectError)
    async def _backplane_error(_: Request, exc: AgentConnectError) -> JSONResponse:
        return JSONResponse(
            status_code=status_for(exc), content={"error": exc.code, "detail": str(exc)}
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, object]:
        """Liveness: the process is up and can answer. Does not touch the ledger,
        so it stays green even while a dependency is degraded — that is readiness's
        job, and conflating them makes an orchestrator kill a pod it should drain."""
        return {
            "status": "ok",
            "workers": [w.worker_id for w in svc.registry.all()],
            "linear_sync": app.state.linear_sync is not None,
            "execution_backend": svc.execution.name,
            "memory_backend": svc.memory.backend_name,
        }

    @app.get("/ready", tags=["meta"])
    def ready() -> JSONResponse:
        """Readiness: can this instance serve real traffic? Probes the ledger with
        a live query. 503 when a hard dependency is down, so a load balancer stops
        routing to it without the orchestrator killing the process."""
        report = svc.readiness()
        code = 200 if report.get("ready") else 503
        return JSONResponse(status_code=code, content={"status": "ok" if report["ready"]
                                                        else "not_ready", **report})

    @app.get("/metrics", tags=["meta"])
    def metrics() -> dict[str, object]:
        """Operational metrics as JSON (sessions/runs/errors/durations/queues).

        JSON rather than Prometheus text is the deliberate pick (ADR 0005): the
        rest of the HTTP surface is JSON, an operator curls it without a scraper,
        and a Prometheus exporter can trivially transcode it. Authenticated —
        counts are ledger data, not a public probe."""
        return svc.metrics()

    for module in (
        routes_tasks, routes_artifacts, routes_reviews, routes_managers,
        routes_subtasks, routes_linear, routes_memory, routes_temporal,
        routes_compliance,
    ):
        app.include_router(module.router)
    return app


#: FastAPI's own surface, which carries no ledger state and no action.
_INTROSPECTION = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def declared_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every `(method, path)` the app serves, flattened.

    Included routers are *nested* objects in this FastAPI version, not entries in
    `app.routes`. Walking only the top level finds `/health` and nothing else — which
    is precisely how a coverage check can pass while covering nothing. Recurse.
    """
    found: list[tuple[str, str]] = []

    def walk(routes: object) -> None:
        for route in routes or ():  # type: ignore[union-attr]
            # FastAPI ≥0.116 wraps `include_router` results in `_IncludedRouter`,
            # whose real `APIRoute`s hang off `original_router`. Older versions nest
            # under `.routes`. Handle both, or a version bump silently empties this.
            included = getattr(route, "original_router", None)
            nested = getattr(included, "routes", None) or getattr(route, "routes", None)
            if nested:
                walk(nested)
                continue
            path = getattr(route, "path", None)
            if not path or path in _INTROSPECTION:
                continue
            for method in getattr(route, "methods", None) or ():
                if method not in ("HEAD", "OPTIONS"):
                    found.append((method, path))

    walk(app.routes)
    return sorted(set(found))


def unmapped_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Routes with no declared action, no public entry, and no webhook exemption.

    Must be empty. A route that appears here is served without `enforce` having
    anything to ask the service about, so it would be reachable with a token minted
    for something else entirely.
    """
    return [key for key in declared_routes(app)
            if key not in PUBLIC_ROUTES
            and key not in WEBHOOK_ROUTES
            and key not in ROUTE_ACTIONS]


def phantom_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Declared actions for routes that do not exist. The `.mcp.json` bug, again.

    A stale entry is not dangerous, but it is a lie about the surface, and lies about
    the surface are how the `get_subtask_status` drift survived for months.
    """
    real = set(declared_routes(app))
    return sorted((set(ROUTE_ACTIONS) | set(WEBHOOK_ROUTES)) - real)


def main() -> None:
    import uvicorn

    import os

    uvicorn.run(
        create_app(),
        host=os.environ.get("AGENTCONNECT_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("AGENTCONNECT_API_PORT", "8790")),
    )


if __name__ == "__main__":
    main()
