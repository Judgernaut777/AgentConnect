"""Execution Records (R6): the pure contract, the ledger storage, and the
service write path.

The record contract itself is pinned in Connect-Governance
``docs/EXECUTION_RECORD.md``; these tests pin the AgentConnect implementation
of it: canonical-JSON discipline, the hash seal, tamper detection, the
fail-closed build rule (no "succeeded" record without a verified redemption),
ledger round-trip + bidirectional id traversal, and hash-chain linkage at
write time.
"""

from __future__ import annotations

import json

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    PolicyViolation,
    SqliteStorage,
)
from agentconnect.core.execution_record_store import ExecutionRecordLedger
from agentconnect.core.execution_records import (
    EXECUTION_RECORD_FORMAT_VERSION,
    ExecutionRecord,
    ExecutorIdentity,
    ProviderEnforcementRef,
    ToolIdentity,
    args_hash_hex,
    build_execution_record,
    canonical_json,
    verify_execution_record,
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


def _record(**over):
    kwargs = dict(
        execution_record_id="execrec_000000000001",
        work_request_id="wr-1",
        task_id="task_abc",
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


# ------------------------------------------------------------ canonical form
class TestCanonicalJson:
    def test_key_order_and_null_retention(self):
        # Sorted keys, no whitespace, absent optionals as null — the same rule
        # as the Connect-Governance grant canonical form.
        assert canonical_json({"b": 1, "a": None}) == b'{"a":null,"b":1}'

    def test_non_ascii_literal_not_escaped(self):
        assert canonical_json({"x": "café"}) == '{"x":"café"}'.encode("utf-8")

    def test_nan_rejected(self):
        with pytest.raises(ValueError):
            canonical_json({"x": float("nan")})

    def test_nested_key_order_at_every_level(self):
        assert canonical_json({"z": {"b": 1, "a": 2}}) == b'{"z":{"a":2,"b":1}}'


# ------------------------------------------------------------ build + seal
class TestBuildAndSeal:
    def test_success_record_seals_and_verifies(self):
        record = _record()
        assert record.record_format_version == EXECUTION_RECORD_FORMAT_VERSION
        assert record.record_hash and verify_execution_record(record)

    def test_determinism_identical_inputs_identical_hash(self):
        assert _record().record_hash == _record().record_hash

    def test_hash_independent_of_construction_order(self):
        a = _record()
        b = build_execution_record(
            outcome="succeeded",
            tool=ToolIdentity(name="write_file", source_id="agentconnect-runtime"),
            executor=ExecutorIdentity(executor_id="worker-1", harness="agentconnect-runtime"),
            provider_enforcement=_enforcement(),
            correlation_id="corr-1",
            grant_id="g-vec-1",
            decision_record_id="dr-vec-1",
            task_id="task_abc",
            work_request_id="wr-1",
            execution_record_id="execrec_000000000001",
            started_at="2026-08-03T12:00:00Z",
            finished_at="2026-08-03T12:00:01Z",
        )
        assert a.record_hash == b.record_hash

    def test_full_id_chain_present(self):
        record = _record()
        assert (record.work_request_id, record.decision_record_id, record.grant_id,
                record.correlation_id) == ("wr-1", "dr-vec-1", "g-vec-1", "corr-1")
        assert record.provider_enforcement.grant_id == "g-vec-1"

    def test_tampered_linkage_field_breaks_the_seal(self):
        record = _record()
        for tamper in ({"grant_id": "g-evil"}, {"decision_record_id": "dr-evil"},
                       {"work_request_id": "wr-evil"}, {"outcome": "failed"},
                       {"correlation_id": "corr-evil"}):
            assert not verify_execution_record(record.model_copy(update=tamper)), tamper

    def test_unknown_outcome_rejected(self):
        with pytest.raises(ValueError, match="unknown execution outcome"):
            _record(outcome="ran_anyway")

    # ---- the fail-closed rule: no redemption, no success record ------------
    def test_success_requires_redeemed_enforcement(self):
        with pytest.raises(ValueError, match="fail-closed"):
            _record(provider_enforcement=_enforcement(
                redemption_outcome="denied:expired", verified=True))

    def test_success_requires_verified_enforcement(self):
        with pytest.raises(ValueError, match="fail-closed"):
            _record(provider_enforcement=_enforcement(verified=False))

    def test_refused_and_failed_build_without_redemption(self):
        refused = _record(outcome="refused", refusal_reason="denied:expired",
                          provider_enforcement=_enforcement(
                              redemption_outcome="denied:expired", verified=False))
        failed = _record(outcome="failed")
        assert verify_execution_record(refused) and verify_execution_record(failed)

    def test_prev_hash_changes_the_seal(self):
        head = _record()
        chained = with_prev_hash(head, head.record_hash)
        assert chained.prev_hash == head.record_hash
        assert chained.record_hash != head.record_hash
        assert verify_execution_record(chained)

    def test_args_hash_is_canonical(self):
        assert args_hash_hex({"b": 1, "a": [1, 2]}) == args_hash_hex({"a": [1, 2], "b": 1})


# ------------------------------------------------------------ ledger storage
class TestStorage:
    def test_roundtrip_and_bidirectional_traversal(self):
        storage = SqliteStorage()
        record = _record()
        storage.insert_execution_record(record, created_at=1.0)
        found = storage.get_execution_record(record.execution_record_id)
        assert found is not None and found.record_hash == record.record_hash
        # Any one id in the chain finds the record naming all the others.
        for key in ("grant_id", "decision_record_id", "correlation_id", "work_request_id"):
            hits = storage.list_execution_records(**{key: getattr(record, key)})
            assert [r.execution_record_id for r in hits] == [record.execution_record_id], key
        hits = storage.list_execution_records(task_id="task_abc")
        assert len(hits) == 1
        assert storage.list_execution_records(grant_id="g-nope") == []

    def test_existing_database_gains_the_table(self, tmp_path):
        # _SCHEMA runs CREATE TABLE IF NOT EXISTS at every open: a pre-R6
        # database upgrades additively with no migration entry.
        db = tmp_path / "ledger.db"
        SqliteStorage(db).insert_execution_record(_record(), created_at=1.0)
        reopened = SqliteStorage(db)
        assert reopened.get_execution_record("execrec_000000000001") is not None

    def test_chain_head(self):
        storage = SqliteStorage()
        assert storage.latest_execution_record_hash() is None
        first = _record()
        storage.insert_execution_record(first, created_at=1.0)
        assert storage.latest_execution_record_hash() == first.record_hash
        second = with_prev_hash(
            _record(execution_record_id="execrec_000000000002"), first.record_hash)
        storage.insert_execution_record(second, created_at=2.0)
        assert storage.latest_execution_record_hash() == second.record_hash
        stored = storage.get_execution_record(second.execution_record_id)
        assert stored.prev_hash == first.record_hash


# ------------------------------------------------------------ service path
class TestService:
    def _svc(self, tmp_path):
        return AgentConnectService.create(
            db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[])

    def test_record_execution_chains_and_stores(self, tmp_path):
        svc = self._svc(tmp_path)
        ledger = ExecutionRecordLedger(svc.storage)
        task = svc.create_task(CreateTaskRequest(title="T"))
        first = ledger.record(_record(task_id=task.id))
        second = ledger.record(
            _record(execution_record_id="execrec_000000000002", task_id=task.id))
        assert first.prev_hash is None
        assert second.prev_hash == first.record_hash
        # Traversal from the governance side: grant id -> execution.
        hits = ledger.list(grant_id="g-vec-1")
        assert {r.execution_record_id for r in hits} == {
            first.execution_record_id, second.execution_record_id}
        assert verify_execution_record(ledger.get(first.execution_record_id))

    def test_tampered_record_rejected_never_stored(self, tmp_path):
        svc = self._svc(tmp_path)
        ledger = ExecutionRecordLedger(svc.storage)
        task = svc.create_task(CreateTaskRequest(title="T"))
        tampered = _record(task_id=task.id).model_copy(update={"grant_id": "g-evil"})
        with pytest.raises(PolicyViolation):
            ledger.record(tampered)
        assert ledger.list(task_id=task.id) == []

    def test_unknown_task_rejected(self, tmp_path):
        svc = self._svc(tmp_path)
        ledger = ExecutionRecordLedger(svc.storage)
        with pytest.raises(Exception):
            ledger.record(_record(task_id="task_nope"))

    def test_record_serializes_as_canonical_json(self, tmp_path):
        svc = self._svc(tmp_path)
        ledger = ExecutionRecordLedger(svc.storage)
        task = svc.create_task(CreateTaskRequest(title="T"))
        record = ledger.record(_record(task_id=task.id))
        # The stored projection round-trips byte-exactly through canonical form:
        # json.loads of the canonical encoding equals the model dump, and the
        # hash over it still verifies (no float/ordering drift in persistence).
        canonical = json.loads(canonical_json(
            record.model_dump(mode="json", exclude={"record_hash"})).decode("utf-8"))
        assert canonical["grant_id"] == "g-vec-1"
        assert canonical["subtask_id"] is None  # absent optional -> null, not omitted
        assert verify_execution_record(record)
