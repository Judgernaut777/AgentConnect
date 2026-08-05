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
from dataclasses import dataclass
from typing import Any, Optional

from .errors import NotFound, PolicyViolation
from .execution_records import (
    ExecutionRecord,
    verify_execution_record,
    with_prev_hash,
)
from .storage import SqliteStorage


@dataclass(frozen=True)
class ChainVerification:
    """Result of a read-side walk of the ledger's hash chain.

    ``ok`` is False at the first break; ``records_checked`` counts the records
    that verified before the break (the whole chain when ``ok``), and
    ``first_break_id`` / ``first_break_reason`` name the record where the walk
    failed (None when ``ok``).
    """

    ok: bool
    records_checked: int
    first_break_id: Optional[str] = None
    first_break_reason: Optional[str] = None


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

    def verify_chain(self) -> ChainVerification:
        """Re-verify the whole ledger on read: every record's seal and every
        ``prev_hash`` link, in insertion order.

        The write path verifies only the incoming record's own seal; this is
        the read-side complement for audit. The first record must carry no
        ``prev_hash``; each later one must name the previous record's
        ``record_hash``. The walk stops at the first break and reports it.

        Known limit of the current schema: unlike ToolConnect's audit table,
        this ledger keeps no durable high-water mark of the chain head, so a
        truncation that removes records off the *tail* leaves every surviving
        record internally consistent and is not detectable here. Removing or
        rewriting any record before the tail breaks the next record's
        ``prev_hash`` link or its own seal and IS detected.
        """
        prev_hash: Optional[str] = None
        records_checked = 0
        for record in self._storage.list_execution_record_chain():
            if (record.prev_hash or None) != prev_hash:
                reason = (
                    "prev_hash mismatch"
                    if records_checked
                    else "first record carries a prev_hash"
                )
                return ChainVerification(
                    False, records_checked, record.execution_record_id, reason)
            if not verify_execution_record(record):
                return ChainVerification(
                    False, records_checked, record.execution_record_id,
                    "record seal does not verify")
            prev_hash = record.record_hash
            records_checked += 1
        return ChainVerification(True, records_checked)
