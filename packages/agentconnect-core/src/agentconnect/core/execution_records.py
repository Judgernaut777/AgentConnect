"""Execution Records (R6) — the third record kind of the ADR-037 taxonomy.

The Connect ecosystem's vertical slice (ADR-048) is a chain of evidence:

    Work Request → Connect Decision Record → signed execution grant (R4)
    → ToolConnect redemption + Provider Enforcement Record (R5)
    → **Execution Record (this module)** → linked audit trail (R7)

An Execution Record is Layer 3 evidence (RA v0.2 §3): *what actually ran, in
what order, with what outcome* — emitted by the Harness (AgentConnect), never
by the governance plane and never by the enforcing provider. It is linkable to
the other two record kinds by id and by ``correlation_id``, and is never
interchangeable with them (ADR-037).

This module is deliberately **pure**: no I/O, no clock, no randomness — the
same isolation discipline as the Connect-Governance Kernel and grant packages.
Callers supply instants (RFC 3339 strings) and identities; the module
canonicalizes, hashes, and validates. Persistence lives in
``core/storage.py`` (the ``execution_records`` ledger table) and emission
wiring in ``agentconnect-runtime``'s act/tool loop.

Fail-closed discipline (normative, pinned by tests): an Execution Record whose
``outcome`` is ``"succeeded"`` is only constructible over a *verified,
redeemed* Provider Enforcement Record. If a grant was required and redemption
failed or never happened, the only records that can be built are
``"failed"``/``"refused"`` ones — a "success" record without a successful
redemption raises at build time rather than existing to be discovered later.

Canonical form is byte-identical to the Connect-Governance grant discipline
(`docs/REDEMPTION_CONTRACT.md` §2): UTF-8, keys sorted by code point, no
insignificant whitespace, non-ASCII literal, absent optionals as JSON ``null``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field

#: Version of the Execution Record shape itself. Additive field additions keep
#: the major; a removed/renamed field or changed meaning bumps it.
EXECUTION_RECORD_FORMAT_VERSION = "1"

#: The only outcomes an Execution Record may carry. ``refused`` means the
#: runtime declined to execute (no successful redemption, wrong-grant echo,
#: governor outage); ``failed`` means execution ran and errored; ``succeeded``
#: means execution ran to completion *and* the governance chain below it was
#: verified + redeemed (enforced by :func:`build_execution_record`).
OUTCOMES = frozenset({"succeeded", "failed", "refused"})


def canonical_json(obj: Any) -> bytes:
    """Canonical JSON encoding — the same rule as Connect-Governance grants.

    UTF-8; object keys sorted lexicographically by code point; separators
    ``,`` and ``:``; non-ASCII emitted literally; ``NaN``/``Infinity`` rejected
    (``allow_nan=False``). Absent optional fields are the caller's ``None``s,
    which encode as JSON ``null`` — never key-dropping.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def args_hash_hex(args: Mapping[str, Any]) -> str:
    """Canonical-JSON SHA-256 of call arguments.

    Used only when the provider's redemption response did not echo an
    ``args_hash`` (the Provider Enforcement Record's hash is authoritative and
    preferred — a Harness records the provider's evidence, not its own
    recomputation, whenever both exist). Raw arguments never appear in an
    Execution Record — only the hash.
    """
    return sha256_hex(canonical_json(dict(args)))


class ProviderEnforcementRef(BaseModel):
    """The R5 Provider Enforcement Record reference embedded in an Execution
    Record (ADR-037 linkage, downward toward the point of effect)."""

    provider_id: str
    grant_id: str
    redemption_outcome: str  # "redeemed" | "denied:<reason>" — provider's own outcome
    verified: bool           # did the provider's signature verification pass
    args_hash: str = ""      # the provider-recorded canonical-args hash
    enforced_at: str = ""    # the instant the provider judged the window against


class ExecutorIdentity(BaseModel):
    """Who executed (Layer 3 vocabulary, per ADR-036 mappings: a worker is not
    a Principal; the linkage to the governance Principal travels via the grant,
    not via this block)."""

    executor_id: str
    executor_kind: str = "worker"
    harness: str = "agentconnect-runtime"


class ToolIdentity(BaseModel):
    source_id: str
    name: str


class ExecutionRecord(BaseModel):
    """One durable, hash-sealed execution fact.

    Identity chain (all traversable in both directions by id):
    ``work_request_id`` (Layer 1 commitment) · ``decision_record_id`` (Layer 1
    Decision Record) · ``grant_id`` (R4 signed grant) · ``correlation_id``
    (cross-record-kind correlator, ADR-037) · ``provider_enforcement``
    (Layer 2 record reference) · ``task_id``/``subtask_id`` (AgentConnect's
    own ledger ids — task/subtask ≠ Work Request, ADR-036; the binding is by
    explicit field, not by conflation).
    """

    execution_record_id: str
    record_format_version: str = EXECUTION_RECORD_FORMAT_VERSION
    work_request_id: str
    task_id: str
    subtask_id: Optional[str] = None
    decision_record_id: str
    grant_id: str
    correlation_id: str
    provider_enforcement: ProviderEnforcementRef
    executor: ExecutorIdentity
    tool: ToolIdentity
    args_hash: str = ""
    outcome: str
    refusal_reason: Optional[str] = None
    started_at: str = ""    # RFC 3339, caller-supplied — this module has no clock
    finished_at: str = ""   # RFC 3339, caller-supplied
    prev_hash: Optional[str] = None   # ledger hash chain; None = chain head
    record_hash: str = ""             # sha256 over canonical JSON of every other field


def _hashable_mapping(record: ExecutionRecord) -> dict[str, Any]:
    """The exact hashed projection: every field except ``record_hash`` itself,
    ``None``s retained as JSON ``null`` (pydantic keeps the keys)."""
    return record.model_dump(mode="json", exclude={"record_hash"})


def seal_execution_record(record: ExecutionRecord) -> ExecutionRecord:
    """Return a copy of ``record`` with ``record_hash`` computed. Idempotent."""
    return record.model_copy(
        update={"record_hash": sha256_hex(canonical_json(_hashable_mapping(record)))})


def verify_execution_record(record: ExecutionRecord) -> bool:
    """True iff ``record_hash`` matches the recomputed hash — tampering with any
    linkage field (ids, outcome, enforcement reference) invalidates the seal."""
    if not record.record_hash:
        return False
    return record.record_hash == sha256_hex(
        canonical_json(_hashable_mapping(record)))


def build_execution_record(
    *,
    execution_record_id: str,
    work_request_id: str,
    task_id: str,
    decision_record_id: str,
    grant_id: str,
    correlation_id: str,
    provider_enforcement: ProviderEnforcementRef,
    executor: ExecutorIdentity,
    tool: ToolIdentity,
    outcome: str,
    subtask_id: Optional[str] = None,
    args_hash: str = "",
    refusal_reason: Optional[str] = None,
    started_at: str = "",
    finished_at: str = "",
    prev_hash: Optional[str] = None,
) -> ExecutionRecord:
    """Build and seal an Execution Record, enforcing the fail-closed rule.

    ``outcome="succeeded"`` requires the embedded Provider Enforcement
    reference to show a verified redemption (``verified`` and
    ``redemption_outcome == "redeemed"``). Anything else raises ``ValueError``
    — where a grant was required, execution without a successful redemption
    must never produce a "success" record, so the record simply cannot be
    built that way.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown execution outcome {outcome!r}")
    if outcome == "succeeded" and not (
        provider_enforcement.verified
        and provider_enforcement.redemption_outcome == "redeemed"
    ):
        raise ValueError(
            "a 'succeeded' Execution Record requires a verified, redeemed "
            "Provider Enforcement Record (fail-closed: no redemption, no success)"
        )
    record = ExecutionRecord(
        execution_record_id=execution_record_id,
        work_request_id=work_request_id,
        task_id=task_id,
        subtask_id=subtask_id,
        decision_record_id=decision_record_id,
        grant_id=grant_id,
        correlation_id=correlation_id,
        provider_enforcement=provider_enforcement,
        executor=executor,
        tool=tool,
        args_hash=args_hash,
        outcome=outcome,
        refusal_reason=refusal_reason,
        started_at=started_at,
        finished_at=finished_at,
        prev_hash=prev_hash,
    )
    return seal_execution_record(record)


def with_prev_hash(record: ExecutionRecord, prev_hash: Optional[str]) -> ExecutionRecord:
    """Re-seal ``record`` onto a ledger chain position. The service uses this
    when persisting: the writer (and only the writer) knows the current chain
    head, so chaining is applied at write time, not at emission time."""
    return seal_execution_record(record.model_copy(update={"prev_hash": prev_hash}))


#: Field-level JSON schema-ish summary, kept next to the code so docs and tests
#: pin the same list (the contract doc cites it; the contract test asserts it).
EXECUTION_RECORD_FIELDS = tuple(ExecutionRecord.model_fields)
