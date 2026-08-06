"""Read-side chain verification for the Execution Record ledger (R7).

The write path (``ExecutionRecordLedger.record``) verifies only the incoming
record's own seal; ``verify_chain()`` is the audit-read complement: it walks
``execution_records`` in insertion order, re-verifies every record's seal via
``verify_execution_record``, and checks every ``prev_hash`` link against the
previous record's ``record_hash``.

Truncation coverage, honestly stated: the schema keeps no durable high-water
mark of the chain head (unlike ToolConnect's ``meta`` audit-head marks), so
deleting records off the *tail* leaves every surviving record internally
consistent and is NOT detectable by ``verify_chain()`` — the final test pins
that known limit rather than pretend otherwise. Deleting or rewriting any
record before the tail IS detected (the next record's ``prev_hash`` no longer
matches, or the record's own seal breaks).
"""

from __future__ import annotations

import json

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    SqliteStorage,
)
from agentconnect.core.execution_record_store import (
    ChainVerification,
    ExecutionRecordLedger,
)
from agentconnect.core.execution_records import (
    ExecutorIdentity,
    ProviderEnforcementRef,
    ToolIdentity,
    build_execution_record,
    with_prev_hash,
)


def _enforcement(**over):
    base = dict(
        provider_id="toolconnect",
        grant_id="g-vec-1",
        redemption_outcome="redeemed",
        verified=True,
        args_hash="ab12" * 16,
        enforced_at="2026-08-03T12:00:00Z",
    )
    base.update(over)
    return ProviderEnforcementRef(**base)


def _record(n: int, task_id: str, **over):
    kwargs = dict(
        execution_record_id=f"execrec_{n:012d}",
        work_request_id="wr-1",
        task_id=task_id,
        decision_record_id="dr-vec-1",
        grant_id="g-vec-1",
        correlation_id="corr-1",
        provider_enforcement=_enforcement(),
        executor=ExecutorIdentity(executor_id="worker-1", harness="agentconnect-runtime"),
        tool=ToolIdentity(source_id="agentconnect-runtime", name="write_file"),
        outcome="succeeded",
        started_at="2026-08-03T12:00:00Z",
        finished_at="2026-08-03T12:00:01Z",
    )
    kwargs.update(over)
    return build_execution_record(**kwargs)


@pytest.fixture
def ledger(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[])
    task = svc.create_task(CreateTaskRequest(title="T"))
    return ExecutionRecordLedger(svc.storage), task, svc.storage


def _chain(ledger, task, n: int):
    return [ledger.record(_record(i, task.id)) for i in range(1, n + 1)]


class TestVerifyChain:
    def test_empty_chain_ok(self, ledger):
        led, _, _ = ledger
        result = led.verify_chain()
        assert result == ChainVerification(True, 0, None, None)

    def test_single_record_ok(self, ledger):
        led, task, storage = ledger
        _chain(led, task, 1)
        result = led.verify_chain()
        assert result.ok and result.records_checked == 1

    def test_multi_record_chain_ok(self, ledger):
        led, task, storage = ledger
        records = _chain(led, task, 5)
        assert records[0].prev_hash is None
        for prev, cur in zip(records, records[1:]):
            assert cur.prev_hash == prev.record_hash
        result = led.verify_chain()
        assert result.ok and result.records_checked == 5

    def test_tampered_record_json_detected(self, ledger):
        # Rewrite a stored record's outcome in place (keeping the stored
        # record_hash): the seal no longer recomputes, and the walk names the
        # tampered record.
        led, task, storage = ledger
        records = _chain(led, task, 3)
        victim = records[1]
        with storage.transaction() as conn:
            row = conn.execute(
                "SELECT record_json FROM execution_records WHERE id=?",
                (victim.execution_record_id,)).fetchone()
            doc = json.loads(row["record_json"])
            doc["outcome"] = "failed"
            conn.execute(
                "UPDATE execution_records SET record_json=? WHERE id=?",
                (json.dumps(doc), victim.execution_record_id))
        result = led.verify_chain()
        assert not result.ok
        assert result.records_checked == 1
        assert result.first_break_id == victim.execution_record_id
        assert "seal" in result.first_break_reason

    def test_broken_prev_hash_link_detected(self, ledger):
        # Insert (bypassing the ledger write path) a record whose seal is valid
        # but whose prev_hash does not name the actual chain head.
        led, task, storage = ledger
        _chain(led, task, 2)
        orphan = with_prev_hash(_record(3, task.id), "ff" * 32)
        storage.insert_execution_record(orphan, created_at=3.0)
        result = led.verify_chain()
        assert not result.ok
        assert result.records_checked == 2
        assert result.first_break_id == orphan.execution_record_id
        assert "prev_hash" in result.first_break_reason

    def test_first_record_with_prev_hash_detected(self, ledger):
        # A chain whose genesis record claims a predecessor is not a chain.
        led, task, storage = ledger
        bad_genesis = with_prev_hash(_record(1, task.id), "00" * 32)
        storage.insert_execution_record(bad_genesis, created_at=1.0)
        result = led.verify_chain()
        assert not result.ok
        assert result.records_checked == 0
        assert result.first_break_id == bad_genesis.execution_record_id
        assert "first record" in result.first_break_reason

    def test_middle_record_deleted_detected(self, ledger):
        led, task, storage = ledger
        records = _chain(led, task, 3)
        with storage.transaction() as conn:
            conn.execute("DELETE FROM execution_records WHERE id=?",
                         (records[1].execution_record_id,))
        result = led.verify_chain()
        assert not result.ok
        assert result.records_checked == 1
        assert result.first_break_id == records[2].execution_record_id
        assert "prev_hash" in result.first_break_reason

    def test_tail_truncation_not_detectable_documented(self, ledger):
        """KNOWN LIMIT, pinned honestly: deleting the newest record leaves a
        shorter chain whose every surviving record still verifies and links —
        with no durable head mark in the schema there is nothing to compare the
        new tip against, so verify_chain() reports ok. ToolConnect's audit
        table solves this with a `meta` high-water mark (audit_head_seq/hash);
        adding that here would be a schema change and is out of R7 scope.
        """
        led, task, storage = ledger
        records = _chain(led, task, 3)
        with storage.transaction() as conn:
            conn.execute("DELETE FROM execution_records WHERE id=?",
                         (records[-1].execution_record_id,))
        result = led.verify_chain()
        assert result.ok and result.records_checked == 2


class TestChainStorageRead:
    def test_chain_read_is_insertion_ordered_and_unbounded(self):
        # The traversal list caps at `limit` (default 100); the chain walk must
        # see every record, oldest first.
        storage = SqliteStorage()
        first = _record(1, "task_abc")
        storage.insert_execution_record(first, created_at=2.0)
        second = with_prev_hash(_record(2, "task_abc"), first.record_hash)
        # Deliberately earlier app-clock timestamp: rowid, not created_at,
        # defines ledger order (the clock is not guaranteed monotone).
        storage.insert_execution_record(second, created_at=1.0)
        chain = storage.list_execution_record_chain()
        assert [r.execution_record_id for r in chain] == [
            first.execution_record_id, second.execution_record_id]
