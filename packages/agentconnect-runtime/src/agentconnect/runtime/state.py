"""Typed state carried through the LangGraph execution loop.

LangGraph channels are last-value-wins per key: each node returns the full new
value for any key it changes (lists are replaced, not merged).
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class RuntimeState(TypedDict, total=False):
    task_id: str
    # Chat transcript sent to the model: system + user + assistant + observations.
    messages: list[dict[str, Any]]
    # Completed act->tool round trips (the max_steps guard counts these).
    iteration: int
    # Action parsed from the model's latest reply; absent until the first act.
    last_action: Optional[dict[str, Any]]
    # Set by finalize.
    done: bool
    status: str
    summary: str
    confidence: float
    changed_artifacts: list[str]
    evidence_refs: list[str]
    risks: list[str]
    recommended_next_action: Optional[str]
    # Token usage accumulated across every act step, so a remote worker can
    # self-report it in WorkerResult.usage (the router cannot meter a worker's
    # own model). Each act node folds in the latest response's counts.
    input_tokens: int
    output_tokens: int
    model_id: Optional[str]
