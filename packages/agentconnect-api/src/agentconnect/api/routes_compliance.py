"""Sessions, workspaces, audit, completion (compliance spec §12–§13, §18).

These are *operator* routes, not agent routes. A launched agent holds a scoped
session token that buys `get_task_context_pack`, `record_attempt`, and friends —
never `complete`. Completion is a decision about whether work was recorded, and
an agent grading its own homework is the failure this whole layer exists to stop.

`complete_task` and `force_complete_task` are in `AGENT_FORBIDDEN_ACTIONS`, so
`authorize()` refuses them to every managed-agent token before a handler runs. The
docstring above is now enforced rather than merely intended.

**Ordinary completion cannot skip the audit.** `force` is not a field on
`CompleteBody`; it lives on a separate endpoint, needs its own action, and demands
a written reason that is recorded in the ledger before the task is touched.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agentconnect.core.errors import InvalidRequest
from agentconnect.core.models import RecordDecisionRequest, ReviewResultRequest

from .authz import principal
from .routes_tasks import service

router = APIRouter(tags=["compliance"])


class LaunchBody(BaseModel):
    manager_id: str
    task_id: Optional[str] = None
    review_id: Optional[str] = None
    claim: bool = False
    readonly: bool = False
    force_readonly: bool = False
    repo_source: Optional[str] = None
    repo_mode: str = "auto"


class CompleteBody(BaseModel):
    """No `completed_by`, and no `force`.

    The actor is the authenticated principal. A body that could name its own actor
    would make the token a formality: anyone holding an operator credential could
    attribute a completion to a colleague, and anyone holding any credential at all
    could claim to be an operator by saying so in JSON.
    """

    summary: str = ""
    content: str = ""


class OverrideBody(CompleteBody):
    """An administrative completion. Audited, attributed, and never silent."""

    reason: str


@router.post("/sessions/launch", status_code=201)
def launch(body: LaunchBody, request: Request) -> dict[str, Any]:
    """Prepare a managed session. The token is returned **once** and never again."""
    result = service(request).launch_session(
        manager_id=body.manager_id, task_id=body.task_id, review_id=body.review_id,
        claim=body.claim, readonly=body.readonly, force_readonly=body.force_readonly,
        repo_source=body.repo_source, repo_mode=body.repo_mode,
        launch_command="POST /sessions/launch",
    )
    return {
        "session": result["session"].model_dump(mode="json"),
        "workspace": result["workspace"].model_dump(mode="json"),
        "claim_id": result["claim_id"], "files": result["files"],
        "token": result["token"], "shell_command": result["shell_command"],
    }


@router.get("/sessions")
def list_sessions(
    request: Request, task_id: Optional[str] = None, manager_id: Optional[str] = None,
    status: Optional[str] = None, limit: int = 50,
) -> list[dict[str, Any]]:
    return [
        s.model_dump(mode="json")
        for s in service(request).list_sessions(task_id, manager_id, status, limit)
    ]


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    return service(request).get_session(session_id).model_dump(mode="json")


@router.post("/sessions/{session_id}/end")
def end_session(session_id: str, request: Request, exit_code: int = 0) -> dict[str, Any]:
    """Ends the session and revokes its token: a leaked env file becomes inert."""
    return service(request).end_shell(session_id, exit_code).model_dump(mode="json")


@router.get("/workspaces")
def list_workspaces(request: Request, include_destroyed: bool = False) -> list[dict[str, Any]]:
    return [
        w.model_dump(mode="json")
        for w in service(request).list_workspaces(include_destroyed)
    ]


@router.get("/workspaces/{workspace_id}")
def get_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
    return service(request).get_workspace(workspace_id).model_dump(mode="json")


@router.get("/tasks/{task_id}/audit")
def audit_task(task_id: str, request: Request) -> dict[str, Any]:
    """Read-only and idempotent: asking twice gives the same answer."""
    return service(request).audit_task(task_id).to_dict()


@router.get("/reviews/{review_id}/audit")
def audit_review(review_id: str, request: Request) -> dict[str, Any]:
    return service(request).audit_review(review_id).to_dict()


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, body: CompleteBody, request: Request) -> dict[str, Any]:
    """Audit first. A failing audit is a `policy_violation` (403), not a 500.

    There is no way to reach `force=True` from here. That is the entire point.
    """
    del body  # the body carries no authority; the principal does
    return service(request).complete_task(task_id, principal(request).actor, force=False)


@router.post("/tasks/{task_id}/complete/override")
def force_complete_task(
    task_id: str, body: OverrideBody, request: Request
) -> dict[str, Any]:
    """Complete a task whose audit does not pass. Operator only, reason required.

    The reason is written to the ledger as a locked decision **before** the task is
    completed, so the override survives even if completion then fails. An override
    that left no trace would be indistinguishable from an audit that passed, which
    is precisely the confusion the audit exists to prevent.
    """
    who = principal(request).actor
    reason = body.reason.strip()
    if not reason:
        raise InvalidRequest("an administrative override must give a reason")

    svc = service(request)
    report = svc.audit_task(task_id)
    svc.record_decision(task_id, RecordDecisionRequest(
        made_by=who,
        decision=f"Administrative completion override by {who}.",
        rationale=reason,
        locked=True,
    ))
    result = svc.complete_task(task_id, who, force=True)
    result["override"] = {
        "reason": reason, "authorized_by": who, "audit_passed": report.passed,
        "failed_checks": [c.name for c in report.checks if c.required and not c.passed],
    }
    return result


@router.post("/reviews/{review_id}/complete")
def complete_review(review_id: str, body: CompleteBody, request: Request) -> dict[str, Any]:
    result = service(request).complete_review_audited(
        review_id,
        ReviewResultRequest(completed_by=principal(request).actor, summary=body.summary,
                            content=body.content),
        force=False,
    )
    return {"review": result["review"].model_dump(mode="json"),
            "audit": result["audit"], "forced": result["forced"]}
