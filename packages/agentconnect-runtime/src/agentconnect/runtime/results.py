"""Convert final runtime state into the shared WorkerResult contract."""

from __future__ import annotations

from agentconnect.common.schemas import SubTask, Usage, WorkerResult

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
        # Sub-tasks delegated during the run (Track 4); empty for a leaf worker.
        subtasks=[SubTask(**s) for s in state.get("subtasks", [])],
        # The loop always ran the model at least once, so report real usage; the
        # router folds it into the task evaluation.
        usage=Usage(
            input_tokens=state.get("input_tokens", 0),
            output_tokens=state.get("output_tokens", 0),
            model_id=state.get("model_id"),
        ),
    )
