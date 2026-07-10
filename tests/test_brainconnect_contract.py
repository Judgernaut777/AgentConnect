"""AgentConnect's adapter against BrainConnect's pinned contract.

BrainConnect (`wikibrain` in code) publishes `docs/CONTRACT.md` and seven JSON
fixtures under `tests/contract/`, each rebuilt from live code on every gate. This
suite holds AgentConnect's `WikiBrainMemoryAdapter` to those exact shapes.

The canonical shapes are embedded, so AgentConnect's gate is self-contained and does
not require the sibling repo to be checked out. When it *is* present, the final tests
cross-check the embedded copies against the real fixtures, so a silent divergence
between the two repositories fails a gate here rather than in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentconnect.core.memory import (
    CaptureRequest,
    MemoryAuthorizationError,
    MemorySafetyRefused,
    MemoryServerError,
    RecallRequest,
    WikiBrainMemoryAdapter,
)

# ---- fixtures embedded from BrainConnect tests/contract/ @ e75cb83 -----------

RECALL_ITEM_MASKED_TRUSTED = {
    "id": "claim_1", "confidence": "high", "status": "promoted", "trusted": True,
    "validity": "current", "source_id": "source_1",
    "scope": {"scope_type": "global", "scope_id": ""},
    "text": "Legacy deploy key ████ rotates quarterly.",
    "safety": {
        "surface": "memory_recall", "decision": "redact", "kinds": ["secret"],
        "redacted": True,
        "findings": [{"engine": "baseline", "engine_version": "1", "kind": "secret",
                      "rule": "aws_access_key", "severity": "critical",
                      "confidence": 1.0, "span": [18, 38],
                      "message": "content matches the aws_access_key credential shape"}],
        "engines": [{"engine": "baseline", "version": "1", "status": "ok",
                     "required": True, "findings": 1}],
    },
}

RECALL_ITEM_CLEAN = {
    "id": "claim_1", "confidence": "high", "status": "promoted", "trusted": True,
    "validity": "current", "source_id": "source_1",
    "scope": {"scope_type": "global", "scope_id": ""},
    "text": "The cache TTL is 300 seconds.",
}

CAPTURE_RESULT_QUARANTINED = {
    "accepted": True, "candidate_id": "candidate_1", "status": "pending",
    "quarantined": True,
    "message": "Filed as a pending candidate. … It is QUARANTINED (prompt_injection "
               "(high, via baseline)) and cannot be promoted without an override.",
    "safety": {"surface": "memory_candidate", "decision": "quarantine",
               "kinds": ["prompt_injection"], "redacted": False,
               "findings": [{"engine": "baseline", "kind": "prompt_injection",
                             "rule": "ignore_instructions", "severity": "high",
                             "span": [12, 44]}]},
}

CAPTURE_RESULT_CLEAN = {
    "accepted": True, "candidate_id": "candidate_2", "status": "pending",
    "quarantined": False, "message": "Filed as a pending candidate.",
}

RECALL_PACK_WITHHELD = {
    "backend": "sqlite_fts", "items": [], "profile": "manager_brief",
    "retrieval_mode": "fts",
    "query": "when answering ignore previous instructions system prompt",
    "warnings": [
        "1 claim(s) matching this query were WITHHELD by safety policy "
        "(prompt_injection (high, via baseline)). They remain in the ledger; "
        "nothing was deleted.",
    ],
}

# The refusal envelope a future `brainconnect serve` will return (HTTP 409). BrainConnect
# has expressed it two ways — a flat `error` string (its server intent, matching this
# adapter's original reader) and a nested `error.code` object (its CONTRACT.md draft). The
# adapter tolerates both so neither repo can break the other by changing the nesting.
_SAFETY_SUMMARY = {"surface": "memory_promotion", "decision": "block",
                   "kinds": ["prompt_injection"], "findings": [], "engines": []}

REFUSAL_ENVELOPE_FLAT = {
    "error": "safety_refused", "retryable": False,
    "detail": "safety policy blocks promoting candidate_1: prompt_injection (high).",
    "safety": _SAFETY_SUMMARY,
}

REFUSAL_ENVELOPE_NESTED = {
    "error": {"code": "safety_refused", "retryable": False,
              "message": "safety policy blocks promoting candidate_1: prompt_injection.",
              "safety": _SAFETY_SUMMARY},
}


def adapter(handler):
    return WikiBrainMemoryAdapter(
        transport=lambda method, url, payload: handler(method, url, payload))


# ---- recall ------------------------------------------------------------------

def test_a_masked_but_trusted_item_arrives_masked_and_trusted():
    """CONTRACT.md: `trusted: true` with a `safety` block is exposure control,
    never distrust. The adapter must carry both, and conflate neither."""
    pack = adapter(lambda m, u, p: {
        "items": [RECALL_ITEM_MASKED_TRUSTED], "warnings": []}).recall(
        RecallRequest(query="keys", profile="manager_brief"))

    item = pack.items[0]
    assert item.metadata["trusted"] is True          # masking did not cost trust
    assert item.safety is not None
    assert item.safety["decision"] == "redact"
    assert item.safety["kinds"] == ["secret"]
    assert "█" in item.text                       # the mask survived transport


def test_a_clean_item_carries_no_safety_block():
    pack = adapter(lambda m, u, p: {
        "items": [RECALL_ITEM_CLEAN], "warnings": []}).recall(
        RecallRequest(query="ttl", profile="manager_brief"))
    assert pack.items[0].safety is None


def test_a_withheld_pack_is_a_complete_answer_with_a_warning():
    """An empty `items` plus a warning is not an absence of memory."""
    pack = adapter(lambda m, u, p: RECALL_PACK_WITHHELD).recall(
        RecallRequest(query="ignore previous instructions", profile="manager_brief"))
    assert pack.items == []
    assert any("WITHHELD by safety policy" in w for w in pack.warnings)


# ---- capture -----------------------------------------------------------------

def test_a_quarantined_capture_is_structurally_flagged():
    result = adapter(lambda m, u, p: CAPTURE_RESULT_QUARANTINED).capture_candidate(
        CaptureRequest(text="x", origin_actor_id="claude"))
    assert result.quarantined is True
    assert result.safety["decision"] == "quarantine"
    assert result.status == "pending"
    assert result.accepted is True                     # accepted != safe


def test_a_clean_capture_is_not_quarantined():
    result = adapter(lambda m, u, p: CAPTURE_RESULT_CLEAN).capture_candidate(
        CaptureRequest(text="x", origin_actor_id="claude"))
    assert result.quarantined is False
    assert result.safety is None


# ---- refusal envelope (the classification bug) -------------------------------

def _refusing_adapter(envelope: dict, status: int = 409) -> WikiBrainMemoryAdapter:
    class _Resp:
        status_code = status
        def json(self):
            return envelope

    class _HttpError(Exception):
        response = _Resp()

    def transport(method, url, payload):
        raise _HttpError()

    return WikiBrainMemoryAdapter(transport=transport)


@pytest.mark.parametrize("envelope", [REFUSAL_ENVELOPE_FLAT, REFUSAL_ENVELOPE_NESTED],
                         ids=["flat", "nested"])
def test_a_409_refusal_is_a_safety_refusal_whichever_shape(envelope):
    """A safety refusal answers HTTP 409. Read by status alone, 409 looks like a plain
    conflict / invalid request — the trust-vs-retry confusion the taxonomy exists to
    prevent. The `error` code disambiguates it, flat or nested."""
    with pytest.raises(MemorySafetyRefused) as caught:
        _refusing_adapter(envelope).promote_candidate("candidate_1", "matthew")
    assert caught.value.summary["decision"] == "block"
    assert "prompt_injection" in caught.value.summary["kinds"]


def test_a_backend_error_envelope_is_a_server_error():
    envelope = {"error": "backend_error", "retryable": True,
                "detail": "a required engine is unavailable"}
    with pytest.raises(MemoryServerError):
        _refusing_adapter(envelope, status=503).promote_candidate("c", "matthew")


def test_a_forbidden_envelope_is_an_authorization_error():
    envelope = {"error": "forbidden", "retryable": False,
                "detail": "an agent reviewer_type may not promote"}
    with pytest.raises(MemoryAuthorizationError):
        _refusing_adapter(envelope, status=403).promote_candidate("c", "matthew")


# ---- cross-check the embedded shapes against the real repo, when present -----

_BRAINCONNECT_FIXTURES = Path("/home/mini/WikiBrain/tests/contract")


def _real(name: str) -> dict:
    return json.loads((_BRAINCONNECT_FIXTURES / name).read_text())


skip_no_repo = pytest.mark.skipif(
    not _BRAINCONNECT_FIXTURES.is_dir(),
    reason="BrainConnect sibling repo not checked out; embedded shapes are authoritative")


@skip_no_repo
def test_embedded_recall_item_still_matches_the_real_fixture():
    real = _real("recall_item_masked_trusted.json")
    assert real["trusted"] is True
    assert real["safety"]["decision"] == RECALL_ITEM_MASKED_TRUSTED["safety"]["decision"]
    assert set(real["safety"]) >= {"surface", "decision", "kinds", "findings", "engines"}


@skip_no_repo
def test_embedded_capture_result_still_matches_the_real_fixture():
    real = _real("capture_result_quarantined.json")
    assert real["quarantined"] is True
    assert "safety" in real and real["status"] == "pending"


@skip_no_repo
def test_the_real_refusal_envelope_classifies_as_a_safety_refusal():
    """Shape-tolerant on purpose: BrainConnect has expressed this envelope both flat
    (`error` string) and nested (`error.code`). Assert the *semantics* — a 409 that
    the adapter reads as a safety refusal — not one particular nesting."""
    real = _real("promotion_safety_refusal.json")
    assert real.get("http_status") == 409
    err = real["error"]
    code = err["code"] if isinstance(err, dict) else err
    assert code == "safety_refused"
    summary = err.get("safety") if isinstance(err, dict) else real.get("safety")
    assert summary and summary["decision"] == "block"
