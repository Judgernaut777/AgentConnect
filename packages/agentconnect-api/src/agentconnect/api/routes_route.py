"""Decision-only routing route (BrainConnect Lane 4 consumer).

`POST /route/decide` exposes the router package's deterministic
``RoutingEngine.route`` as a bare, **side-effect-free** query. It is the endpoint
BrainConnect's ``HttpRoutingClient`` calls: it POSTs a routing *context* and gets
back a ``RoutingDecision`` — the same explainable record AgentConnect computes
internally — without any of the execution ``submit_task``/``enqueue_task`` perform.

What this route deliberately does NOT do (the whole point of it existing):

* it does not dispatch a model generation,
* it does not enqueue a task or persist a Subtask,
* it does not record a routing decision in the ledger or mutate any state.

It answers "where would this route?" and nothing else. Authentication is the
app-level ``enforce`` dependency like every other route; the declared action is
``decide_route`` (see ``authz.ROUTE_ACTIONS``), so an unauthenticated caller is
rejected with 401 before the handler runs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agentconnect.common.schemas import Priority, PrivacyClass, RoutingDecision
from agentconnect.router.routing import RoutingContext

router = APIRouter(tags=["route"])


class RouteDecideBody(BaseModel):
    """The routing context BrainConnect POSTs. Mirrors ``RoutingContext`` field for
    field, so a malformed body is a clean 422 from FastAPI's validation rather than
    a 500 from constructing the dataclass with junk."""

    task_id: str
    privacy_class: PrivacyClass
    needed_capabilities: list[str] = Field(default_factory=list)
    profile: Optional[str] = None
    require_exact_model: Optional[str] = None
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    allow_external: bool = True
    allow_paid: bool = False
    priority: Priority = Priority.normal
    quality: str = "standard"  # standard | high | best_effort
    cloud_safe: bool = True
    pending_same_model_batch: int = 0
    allow_rented: bool = False


def _router(request: Request):
    """The decision-only RouterService, or a 503 if the deployment could not build
    one (missing router package / config) — the same posture ``/linear/*`` uses."""
    built = getattr(request.app.state, "router", None)
    if built is None:
        raise HTTPException(
            status_code=503,
            detail="router is not configured; the decision-only route is unavailable",
        )
    return built


@router.post("/route/decide", response_model=RoutingDecision)
def decide_route(body: RouteDecideBody, request: Request) -> RoutingDecision:
    """Compute a routing decision for ``body`` without executing anything."""
    ctx = RoutingContext(
        task_id=body.task_id,
        privacy_class=body.privacy_class,
        needed_capabilities=tuple(body.needed_capabilities),
        profile=body.profile,
        require_exact_model=body.require_exact_model,
        est_input_tokens=body.est_input_tokens,
        est_output_tokens=body.est_output_tokens,
        allow_external=body.allow_external,
        allow_paid=body.allow_paid,
        priority=body.priority,
        quality=body.quality,
        cloud_safe=body.cloud_safe,
        pending_same_model_batch=body.pending_same_model_batch,
        allow_rented=body.allow_rented,
    )
    return _router(request).decide_route(ctx)
