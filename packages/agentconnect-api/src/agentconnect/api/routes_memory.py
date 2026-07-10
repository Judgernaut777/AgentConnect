"""Memory routes (adapters spec, Part A).

These call `AgentConnectService`, never a memory backend directly — that is what
keeps visibility policy (trusted_only, pending labelling, item caps) in one place
instead of scattered across callers.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from agentconnect.core.memory import (
    CaptureRequest,
    MemoryFeedbackRequest,
    MemoryScope,
    RecallRequest,
)

from .authz import assert_actor, principal
from .routes_tasks import service

router = APIRouter(tags=["memory"])


class ScopeBody(BaseModel):
    scope_type: str
    scope_id: str


class RecallBody(BaseModel):
    query: str
    task_id: Optional[str] = None
    profile: str = "manager_brief"
    scopes: list[ScopeBody] = []
    max_items: int = 8
    trusted_only: bool = True
    include_pending: bool = False
    include_superseded: bool = False


class CaptureBody(BaseModel):
    text: str
    task_id: Optional[str] = None
    origin_actor_id: Optional[str] = None
    origin_actor_type: Optional[str] = None
    source_ref: Optional[str] = None
    tags: list[str] = []


class FeedbackBody(BaseModel):
    feedback: str
    task_id: Optional[str] = None
    memory_item_id: Optional[str] = None
    source_id: Optional[str] = None
    actor_id: Optional[str] = None
    note: Optional[str] = None


def _pack(pack) -> dict[str, Any]:
    return {
        "backend": pack.backend, "profile": pack.profile, "query": pack.query,
        "warnings": pack.warnings,
        "items": [
            {"text": i.text, "status": i.status, "confidence": i.confidence,
             "source_id": i.source_id, "source_url": i.source_url,
             "superseded_by": i.superseded_by,
             "backend": (i.metadata or {}).get("backend"),
             "role": (i.metadata or {}).get("role"),
             "trusted": (i.metadata or {}).get("trusted", False)}
            for i in pack.items
        ],
    }


@router.post("/memory/recall")
def recall(body: RecallBody, request: Request) -> dict[str, Any]:
    pack = service(request).recall_memory(RecallRequest(
        query=body.query, task_id=body.task_id, profile=body.profile,  # type: ignore[arg-type]
        scopes=[MemoryScope(s.scope_type, s.scope_id) for s in body.scopes],
        max_items=body.max_items, trusted_only=body.trusted_only,
        include_pending=body.include_pending, include_superseded=body.include_superseded,
    ))
    return _pack(pack)


@router.post("/memory/capture")
def capture(body: CaptureBody, request: Request) -> dict[str, Any]:
    who = assert_actor(request, body.origin_actor_id)
    result = service(request).capture_memory_candidate(CaptureRequest(
        text=body.text, task_id=body.task_id, origin_actor_id=who,
        origin_actor_type=body.origin_actor_type, source_ref=body.source_ref, tags=body.tags,
    ))
    return {
        "accepted": result.accepted, "candidate_id": result.candidate_id,
        "status": result.status, "message": result.message, "backend": result.backend,
        # A quarantined candidate is stored but may never be promoted. It must be
        # structurally distinguishable from an ordinary pending one — a manager
        # reading `status: "pending"` and nothing else would queue it for review.
        "quarantined": result.quarantined, "safety": result.safety,
    }


@router.post("/memory/feedback", status_code=202)
def feedback(body: FeedbackBody, request: Request) -> dict[str, Any]:
    service(request).record_memory_feedback(MemoryFeedbackRequest(
        task_id=body.task_id, memory_item_id=body.memory_item_id, source_id=body.source_id,
        feedback=body.feedback, actor_id=body.actor_id, note=body.note,
    ))
    return {"recorded": True}


class PromoteBody(BaseModel):
    candidate_id: str
    promoted_by: str


@router.get("/memory/pending")
def pending(request: Request, limit: int = 50) -> dict[str, Any]:
    """The librarian's queue: candidates awaiting a human promotion decision."""
    return {"candidates": service(request).list_pending_memory(limit)}


@router.post("/memory/promote")
def promote(body: PromoteBody, request: Request) -> dict[str, Any]:
    """Human/librarian only. Deliberately absent from the MCP surface: an agent
    must never be able to promote its own suggestion into trusted memory.

    `promote_memory_candidate` is in `AGENT_FORBIDDEN_ACTIONS`, so `authorize()`
    refuses this route to every managed-agent token whatever its scope claims.
    Safety override is **not** exposed here: overriding a safety refusal is a human
    judgement about content, and it is made at the librarian's own console.
    """
    who = assert_actor(request, body.promoted_by)
    return service(request).promote_memory_candidate(body.candidate_id, who)


@router.get("/memory/health")
def health(request: Request) -> dict[str, Any]:
    return service(request).memory_health()


@router.get("/tasks/{task_id}/context-pack")
def context_pack(
    task_id: str, request: Request, profile: str = "manager_brief",
    max_memory_items: Optional[int] = None, manager_id: Optional[str] = None,
    include_pending: bool = False, worker_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> dict[str, Any]:
    pack = service(request).get_task_context_pack(
        task_id, profile=profile, max_memory_items=max_memory_items,
        manager_id=manager_id, include_pending=include_pending,
        worker_id=worker_id, model_id=model_id,
    )
    return {
        "task_id": pack.task_id, "profile": pack.profile,
        # worker_brief carries no handoff: a bounded worker gets its subtask and
        # its constraints, never the manager's debate.
        "handoff": pack.handoff.model_dump(mode="json") if pack.handoff else None,
        "backends_queried": pack.backends_queried,
        "scopes_queried": pack.scopes_queried,
        "memory": _pack(pack.memory),
        "warnings": pack.warnings,
        "memory_is_external_context": pack.memory_is_external_context,
    }
