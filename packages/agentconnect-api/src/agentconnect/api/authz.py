"""HTTP authentication and authorization.

The transport authenticates. `AgentConnectService.authorize` decides. Nothing in
this module contains a policy rule — it turns a request into `(token, action,
task_id, review_id)` and hands that to the service, which owns every rule.

Two properties are worth stating because they are easy to lose:

**The route table is exhaustive by construction.** A route with no entry in
`ROUTE_ACTIONS` is refused with 500 rather than served, so adding a route without
deciding its action fails loudly instead of shipping an open door. A startup test
asserts the table covers the app.

**The actor is the token, never the body.** A caller who says
``{"completed_by": "matthew"}`` is making a claim about identity, and a claim is
not a credential. Handlers read `request.state.principal.actor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException, Request

#: The only routes served without a token. Liveness, and nothing that names a task.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"),
})

#: (method, path template) -> the action `authorize()` is asked about.
#:
#: Read routes get an action too. Authentication is not only about mutation: a task
#: goal, an artifact body, and a context pack are exactly the things an unscoped
#: reader should not be able to enumerate.
ROUTE_ACTIONS: dict[tuple[str, str], str] = {
    # tasks
    ("POST", "/tasks"): "create_task",
    ("GET", "/tasks"): "list_tasks",
    ("GET", "/tasks/{task_id}"): "get_task",
    ("GET", "/tasks/{task_id}/handoff"): "get_task",
    ("POST", "/tasks/{task_id}/handoff/regenerate"): "get_handoff_summary",
    ("POST", "/tasks/{task_id}/claim"): "claim_task",
    ("POST", "/tasks/{task_id}/release"): "release_task",
    ("POST", "/tasks/{task_id}/constraints"): "add_constraint",
    ("POST", "/tasks/{task_id}/decisions"): "record_decision",
    ("POST", "/tasks/{task_id}/attempts"): "record_attempt",
    # subtasks
    ("POST", "/tasks/{task_id}/subtasks"): "submit_subtask",
    ("GET", "/subtasks/{subtask_id}"): "get_subtask_status",
    ("POST", "/subtasks/{subtask_id}/cancel"): "cancel_subtask",
    ("GET", "/subtasks/{subtask_id}/route"): "explain_route",
    ("POST", "/subtasks/{subtask_id}/approve"): "approve_subtask",
    ("POST", "/subtasks/{subtask_id}/deny"): "deny_subtask",
    # reviews
    ("POST", "/tasks/{task_id}/reviews"): "request_review",
    ("GET", "/reviews/{review_id}"): "get_review",
    ("POST", "/reviews/{review_id}/claim"): "claim_review",
    ("POST", "/reviews/{review_id}/result"): "complete_review",
    # artifacts
    ("POST", "/tasks/{task_id}/artifacts"): "register_artifact",
    ("GET", "/tasks/{task_id}/artifacts"): "list_artifacts",
    ("GET", "/artifacts/{artifact_id}"): "read_artifact_chunk",
    ("GET", "/artifacts/{artifact_id}/chunk"): "read_artifact_chunk",
    # memory
    ("POST", "/memory/recall"): "recall_memory",
    ("POST", "/memory/capture"): "capture_memory_candidate",
    ("POST", "/memory/feedback"): "record_memory_feedback",
    ("GET", "/memory/pending"): "list_pending_memory",
    ("POST", "/memory/promote"): "promote_memory_candidate",
    ("GET", "/memory/health"): "get_status",
    ("GET", "/tasks/{task_id}/context-pack"): "get_task_context_pack",
    # managers
    ("GET", "/managers/{manager_id}/inbox"): "get_inbox",
    # linear
    ("POST", "/linear/sync"): "linear_sync",
    ("GET", "/linear/tasks/{task_id}"): "get_task",
    # temporal
    ("GET", "/workflows/{workflow_id}"): "get_execution_status",
    ("POST", "/workflows/{workflow_id}/signal"): "temporal_signal",
    # compliance — operator surface
    ("POST", "/sessions/launch"): "launch_session",
    ("GET", "/sessions"): "list_sessions",
    ("GET", "/sessions/{session_id}"): "list_sessions",
    ("POST", "/sessions/{session_id}/end"): "end_session",
    ("GET", "/workspaces"): "list_workspaces",
    ("GET", "/workspaces/{workspace_id}"): "list_workspaces",
    ("GET", "/tasks/{task_id}/audit"): "audit_task",
    ("GET", "/reviews/{review_id}/audit"): "audit_review",
    ("POST", "/tasks/{task_id}/complete"): "complete_task",
    ("POST", "/tasks/{task_id}/complete/override"): "force_complete_task",
    ("POST", "/reviews/{review_id}/complete"): "complete_review",
}

#: Routes exempt from Linear's unauthenticated webhook problem are *not* exempt here.
#: The webhook is signed by Linear, not by us, and it is registered separately below.
WEBHOOK_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/linear/webhook"),
})


@dataclass(frozen=True)
class Principal:
    """Who the request is, as established by the token. Never by the body."""

    actor: str
    mode: str
    session_id: Optional[str]
    task_id: Optional[str]
    review_id: Optional[str]
    actions: frozenset[str]

    @property
    def is_operator(self) -> bool:
        return self.mode == "operator"

    @classmethod
    def from_scope(cls, scope: dict[str, Any]) -> "Principal":
        return cls(
            actor=str(scope.get("manager_id") or "unknown"),
            mode=str(scope.get("mode") or "unknown"),
            session_id=scope.get("session_id"),
            task_id=scope.get("task_id"),
            review_id=scope.get("review_id"),
            actions=frozenset(scope.get("actions", [])),
        )


def bearer_token(request: Request) -> Optional[str]:
    """`Authorization: Bearer act_…`, or the `X-AgentConnect-Token` header.

    Both are accepted because `launch` writes the raw token into
    `.env.agentconnect` as `AGENTCONNECT_SESSION_TOKEN`, and a shell script reaching
    for `curl -H "X-AgentConnect-Token: $AGENTCONNECT_SESSION_TOKEN"` should not have
    to learn a header grammar. A malformed `Authorization` header is an error, not a
    reason to fall through and try the other one.
    """
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            raise HTTPException(401, {"error": "unauthenticated",
                                      "detail": "malformed Authorization header"})
        return value.strip()
    direct = request.headers.get("x-agentconnect-token")
    return direct.strip() if direct and direct.strip() else None


def _route_key(request: Request) -> Optional[tuple[str, str]]:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return (request.method.upper(), path) if path else None


async def enforce(request: Request) -> None:
    """Applied to every route. Authenticate, then delegate the decision.

    Registered as an app-level dependency, so a new route is covered the moment it
    exists — the failure mode of a per-route decorator is the route someone forgot
    to decorate.
    """
    key = _route_key(request)
    if key is None or key in PUBLIC_ROUTES or key in WEBHOOK_ROUTES:
        return

    action = ROUTE_ACTIONS.get(key)
    if action is None:
        # Fail closed. An unmapped route is a route nobody decided the policy for.
        raise HTTPException(500, {
            "error": "unmapped_route",
            "detail": f"no authorization action is declared for {key[0]} {key[1]}",
        })

    token = bearer_token(request)
    if not token:
        raise HTTPException(401, {"error": "unauthenticated",
                                  "detail": "a session token is required"})

    params = request.path_params
    service = request.app.state.service
    # PolicyViolation -> 403 through the app's AgentConnectError handler. Only the
    # *absence* of a usable credential is a 401; a real token doing a forbidden
    # thing is a 403, and the difference tells an operator which one to fix.
    scope = service.authorize(
        token, action,
        task_id=params.get("task_id"), review_id=params.get("review_id"),
    )
    request.state.principal = Principal.from_scope(scope)


def principal(request: Request) -> Principal:
    """The authenticated principal. Present on every non-public route."""
    found = getattr(request.state, "principal", None)
    if found is None:  # pragma: no cover — enforce() runs first, always
        raise HTTPException(401, {"error": "unauthenticated", "detail": "no principal"})
    return found


def assert_actor(request: Request, claimed: Optional[str]) -> str:
    """The body may *name* an actor; an agent may not *become* one.

    Where a request carries an actor — `made_by`, `actor_id`, `manager_id` — a
    managed agent's must match its token's. Otherwise a manager token issued to
    `agent-a` writes attempts as `agent-b`, and the ledger's account of who did what
    degrades into a record of what each caller was willing to type.

    An **operator** may name another actor, because that is what a control plane
    does: it launches sessions for agents, claims on their behalf, and reconciles
    their work. Its authority came from the token, not from the name in the body.
    Note that completion routes ignore the body's actor for *everyone* — there the
    attribution is the authority, so it is taken from the principal alone.

    Returns the actor to record.
    """
    from agentconnect.core.errors import PolicyViolation

    who = principal(request)
    if not claimed or claimed == who.actor:
        return who.actor
    if who.is_operator:
        return claimed
    raise PolicyViolation(
        f"this token authenticates {who.actor!r}; it cannot act as {claimed!r}"
    )
