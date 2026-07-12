"""Append-only JSONL observability (production handoff Part II + Part V).

One event, one line, forever appended. This is the always-on local record: even
when a live provider (tmux, Herdr) is also configured, the JSONL file is the
durable trace a human greps after the fact and the CLI reads for `agents events`.

Why JSONL and not the ledger's `events` table: an observation event is *derived*
from the ledger, carries the full correlation id set, and is deliberately outside
the transactional task state so that writing it can never be on the critical path
of a task mutation. A provider failure here is isolated (Part IV) — the ledger is
untouched whatever happens to the log file.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from ..model import (
    AgentObservationEvent,
    ObservationHandle,
    ObservationOutcome,
    ProviderHealth,
    SessionObservationRequest,
    SpawnObservationRequest,
    StateObservationRequest,
)
from ..provider import AgentObservabilityProvider


class StructuredLogObservabilityProvider(AgentObservabilityProvider):
    """Append every event as one JSON object per line.

    Writes are serialized under a lock and flushed per line, so a reader tailing
    the file never sees a half-written record. Ordering on disk is arrival order;
    a reader restores logical order with the ``sequence`` field.
    """

    name = "structured_log"

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        parent = Path(self.path).parent
        writable = os.access(parent, os.W_OK)
        return ProviderHealth(
            provider=self.name, available=writable,
            detail=f"jsonl at {self.path}" if writable else f"{parent} not writable",
            metadata={"path": self.path},
        )

    # ---------------------------------------------------------------- begin
    def create_session(self, request: SessionObservationRequest) -> ObservationHandle:
        return ObservationHandle(
            provider=self.name, handle_id=f"log:{request.session_id}", kind="session",
            delegation_id=request.delegation_id, trace_id=request.trace_id,
            task_id=request.task_id, target=self.path,
        )

    def spawn_process(self, request: SpawnObservationRequest) -> ObservationHandle:
        anchor = request.run_id or request.subtask_id or request.trace_id
        return ObservationHandle(
            provider=self.name, handle_id=f"log:{anchor}", kind="process",
            delegation_id=request.delegation_id, trace_id=request.trace_id,
            task_id=request.task_id, target=self.path,
        )

    # ---------------------------------------------------------------- report
    def append_event(self, event: AgentObservationEvent) -> None:
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    def update_state(self, request: StateObservationRequest) -> None:
        # State changes ride the event stream; a bare state ping is not logged
        # to avoid a second, id-less record for the same transition.
        return None

    # ----------------------------------------------------------------- read
    def read_events(
        self, trace_id: Optional[str] = None, task_id: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        """Read back events, newest-last, optionally filtered and re-sorted by
        `(sequence, timestamp)` so an out-of-order file reads in logical order."""
        if not os.path.exists(self.path):
            return []
        rows: list[dict] = []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if trace_id is not None and obj.get("trace_id") != trace_id:
                        continue
                    if task_id is not None and obj.get("task_id") != task_id:
                        continue
                    rows.append(obj)
        rows.sort(key=lambda o: (o.get("sequence", 0), o.get("timestamp", 0.0)))
        return rows[-limit:]

    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        return None
