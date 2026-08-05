"""The ledger door for Execution Records (R6).

Follows the **event-log provider** precedent rather than adding surface to
``AgentConnectService``: observability/event-log providers already wrap a
``SqliteStorage`` directly (they publish to ``event_log`` without going
through service methods), and Execution Records are the same kind of traffic —
append-only evidence, written by the runtime's emission sink, read by audit
traversal. This class is that door: it owns the write-time concerns the pure
``execution_records`` module deliberately lacks (a clock for ledger ordering,
the hash-chain head, the write transaction).

Wiring (production): ``ExecutionRecordLedger(service.storage).record`` is the
``sink`` a ``GovernanceLinkage`` carries. Tests pass ``list.append`` instead —
the sink seam keeps the runtime free of any storage dependency.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .errors import NotFound, PolicyViolation
from .execution_records import (
    ExecutionRecord,
    verify_execution_record,
    with_prev_hash,
)
from .storage import SqliteStorage


class ExecutionRecordLedger:
    """Append-only write + traversal reads for Execution Records.

    Write path: verify the caller's seal, attach the ledger hash-chain
    position (``prev_hash`` = current head, read and written inside ONE
    storage transaction so the chain cannot fork under concurrency), insert.
    A record whose hash does not verify is rejected, never stored — the ledger
    is the evidence of record and must not launder tampered linkage.
    """

    def __init__(self, storage: SqliteStorage, *, clock=time.time) -> None:
        self._storage = storage
        #: App-layer clock for ledger ordering only (``created_at``); the
        #: record's own timestamps are the emitter's, sealed inside the hash.
        self._clock = clock

    def record(self, record: ExecutionRecord) -> ExecutionRecord:
        if self._storage.get_task(record.task_id) is None:
            raise NotFound(f"unknown task {record.task_id!r}")
        if not verify_execution_record(record):
            raise PolicyViolation(
                f"execution record {record.execution_record_id!r} fails hash verification"
            )
        with self._storage.transaction() as conn:
            chained = with_prev_hash(
                record, self._storage.latest_execution_record_hash(conn=conn))
            self._storage.insert_execution_record(chained, self._clock(), conn=conn)
        return chained

    def get(self, execution_record_id: str) -> Optional[ExecutionRecord]:
        return self._storage.get_execution_record(execution_record_id)

    def list(self, task_id: Optional[str] = None, **filters: Any) -> list[ExecutionRecord]:
        """Traversal reads for the ADR-048 chain: filter by any of
        ``grant_id`` / ``decision_record_id`` / ``correlation_id`` /
        ``work_request_id`` (keyword) — given any one id in the chain, the
        records naming all the others come back."""
        return self._storage.list_execution_records(task_id, **filters)
