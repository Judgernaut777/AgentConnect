"""Convert final runtime state into the shared WorkerResult contract."""

from __future__ import annotations

from agentconnect.common.schemas import WorkerResult

from .state import RuntimeState


def worker_result_from_state(state: RuntimeState) -> WorkerResult:
    return WorkerResult(
        status=state.get("status", "incomplete"),
        summary=state.get("summary", ""),
        confidence=state.get("confidence", 0.0),
        changed_artifacts=list(state.get("changed_artifacts", [])),
        evidence_refs=list(state.get("evidence_refs", [])),
        risks=list(state.get("risks", [])),
        recommended_next_action=state.get("recommended_next_action"),
    )
