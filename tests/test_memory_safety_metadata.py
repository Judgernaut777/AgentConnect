"""BrainConnect's safety information survives the adapter.

BrainConnect (still `wikibrain` in code) scans on recall and again at promotion. It
tells us *why* it masked a claim, *that* a candidate is quarantined, and *when* its
safety policy refuses a promotion outright.

The adapter used to drop all three. A manager saw a shorter pack with no explanation,
a quarantined candidate was indistinguishable from an ordinary pending one, and a
safety refusal arrived as a bare exception that read exactly like a dropped socket.

Two rules the tests below pin down:

* **Safety never touches trust.** A flagged claim may still be trusted; a clean one
  may not be. `trusted` comes from the authority's own verdict and nothing else.
* **Quarantine is structural.** It is a field, never a substring of `message`.
"""

from __future__ import annotations

import pytest

from agentconnect.core.errors import InvalidRequest
from agentconnect.core.memory import (
    CaptureRequest,
    MemoryAuthorizationError,
    MemorySafetyRefused,
    MemoryServerError,
    MemoryUnavailable,
    RecallRequest,
    WikiBrainMemoryAdapter,
)

CLAIM = {
    "id": "claim_1",
    "text": "The refresh token lives in [REDACTED:secret].",
    "status": "promoted",
    "confidence": "high",
    "trusted": True,
    "safety": {"decision": "redact", "risk_level": "high",
               "findings": [{"rule_id": "detect_secrets.keyword", "category": "secret"}]},
}


def adapter(handler):
    return WikiBrainMemoryAdapter(transport=lambda method, url, payload: handler(method, url, payload))


# ------------------------------------------------------------------- recall

def test_a_per_item_safety_verdict_survives_recall():
    pack = adapter(lambda m, u, p: {"items": [CLAIM], "warnings": []}).recall(
        RecallRequest(query="tokens", profile="manager_brief"))

    item = pack.items[0]
    assert item.safety is not None
    assert item.safety["decision"] == "redact"
    assert item.safety["findings"][0]["category"] == "secret"


def test_safety_metadata_cannot_make_an_untrusted_claim_trusted():
    """A clean scan is not a promotion. The authority's `trusted` is the only signal."""
    untrusted = dict(CLAIM, trusted=False, status="pending",
                     safety={"decision": "allow", "risk_level": "none"})
    pack = adapter(lambda m, u, p: {"items": [untrusted], "warnings": []}).recall(
        RecallRequest(query="q", profile="manager_brief", trusted_only=False,
                      include_pending=True))

    item = pack.items[0]
    assert item.safety["decision"] == "allow"
    assert item.metadata["trusted"] is False
    assert item.metadata["authority_trusted"] is False


def test_a_flagged_claim_may_still_be_trusted():
    """Safety and trust are orthogonal axes, and the adapter must not conflate them."""
    pack = adapter(lambda m, u, p: {"items": [CLAIM], "warnings": []}).recall(
        RecallRequest(query="q", profile="manager_brief"))

    assert pack.items[0].safety["risk_level"] == "high"
    assert pack.items[0].metadata["trusted"] is True
    assert pack.items[0].metadata["authority_trusted"] is True


def test_an_item_without_a_safety_block_reports_none_not_an_empty_dict():
    clean = {k: v for k, v in CLAIM.items() if k != "safety"}
    pack = adapter(lambda m, u, p: {"items": [clean], "warnings": []}).recall(
        RecallRequest(query="q", profile="manager_brief"))
    assert pack.items[0].safety is None


def test_the_authoritys_own_recall_warnings_reach_the_caller():
    pack = adapter(lambda m, u, p: {
        "items": [], "warnings": ["1 item withheld by BrainConnect safety"]}).recall(
        RecallRequest(query="q", profile="manager_brief"))
    assert "withheld by BrainConnect safety" in pack.warnings[0]


# ------------------------------------------------------------------ capture

def test_a_quarantined_candidate_is_structurally_distinguishable():
    result = adapter(lambda m, u, p: {
        "accepted": True, "candidate_id": "cand_1", "status": "pending",
        "message": "stored, but quarantined", "quarantined": True,
        "safety": {"decision": "quarantine", "risk_level": "high"},
    }).capture_candidate(CaptureRequest(text="x", origin_actor_id="claude"))

    assert result.quarantined is True
    assert result.safety["decision"] == "quarantine"
    assert result.status == "pending"


def test_an_ordinary_pending_candidate_is_not_quarantined():
    result = adapter(lambda m, u, p: {
        "accepted": True, "candidate_id": "cand_2", "status": "pending",
    }).capture_candidate(CaptureRequest(text="x", origin_actor_id="claude"))

    assert result.quarantined is False
    assert result.safety is None


def test_quarantine_is_never_inferred_from_the_human_readable_message():
    """The word appears in the prose and nowhere else. It must not be believed."""
    result = adapter(lambda m, u, p: {
        "accepted": True, "candidate_id": "c", "status": "pending",
        "message": "this candidate was quarantined, honest",
    }).capture_candidate(CaptureRequest(text="x", origin_actor_id="claude"))

    assert result.quarantined is False


def test_capture_still_cannot_report_a_promotion():
    result = adapter(lambda m, u, p: {
        "accepted": True, "candidate_id": "c", "status": "promoted",
    }).capture_candidate(CaptureRequest(text="x", origin_actor_id="claude"))
    assert result.status == "pending"


# ---------------------------------------------------------------- promotion

class _FakeSafetyResult:
    def summary(self):
        return {"decision": "block", "risk_level": "high",
                "findings": [{"rule_id": "baseline.aws_key", "category": "secret"}]}


class SafetyRefused(Exception):
    """Structurally what BrainConnect raises; recognized by name, not by import."""

    def __init__(self, message):
        super().__init__(message)
        self.result = _FakeSafetyResult()


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _HttpStatusError(Exception):
    def __init__(self, response):
        super().__init__("http error")
        self.response = response


def _raising(exc):
    def transport(method, url, payload):
        raise exc
    return WikiBrainMemoryAdapter(transport=transport)


def test_a_safety_refusal_is_not_a_transport_failure():
    with pytest.raises(MemorySafetyRefused) as caught:
        _raising(SafetyRefused("safety policy blocks promoting cand_4")).promote_candidate(
            "cand_4", "matthew")

    assert caught.value.summary["decision"] == "block"
    assert caught.value.summary["findings"][0]["category"] == "secret"
    assert "cand_4" in str(caught.value)


def test_a_safety_refusal_over_http_is_recognized_by_its_error_code():
    body = {"error": "safety_refused", "detail": "secret (high)",
            "safety": {"decision": "block"}}
    with pytest.raises(MemorySafetyRefused) as caught:
        _raising(_HttpStatusError(_Response(422, body))).promote_candidate("c", "matthew")
    assert caught.value.summary == {"decision": "block"}


@pytest.mark.parametrize("status,expected", [
    (401, MemoryAuthorizationError),
    (403, MemoryAuthorizationError),
    (404, InvalidRequest),
    (409, InvalidRequest),
    (500, MemoryServerError),
    (503, MemoryServerError),
])
def test_backend_failures_are_told_apart(status, expected):
    with pytest.raises(expected):
        _raising(_HttpStatusError(_Response(status))).promote_candidate("c", "matthew")


def test_an_unreachable_backend_is_not_a_safety_refusal():
    class ConnectError(Exception):
        pass

    with pytest.raises(MemoryUnavailable):
        _raising(ConnectError("connection refused")).promote_candidate("c", "matthew")


def test_an_unrecognized_failure_is_re_raised_rather_than_relabelled():
    """Guessing wrong about a failure is worse than declining to name it."""
    class Weird(Exception):
        pass

    with pytest.raises(Weird):
        _raising(Weird("?")).promote_candidate("c", "matthew")


def test_promotion_forwards_a_safety_override_only_with_a_reason():
    seen: dict = {}

    def transport(method, url, payload):
        seen.update(payload or {})
        return {"claim_id": "claim_9"}

    a = WikiBrainMemoryAdapter(transport=transport)

    with pytest.raises(InvalidRequest, match="written reason"):
        a.promote_candidate("c", "matthew", safety_override=True)
    with pytest.raises(InvalidRequest, match="written reason"):
        a.promote_candidate("c", "matthew", safety_override=True, override_reason="   ")

    a.promote_candidate("c", "matthew", safety_override=True,
                        override_reason="reviewed by hand; the key is revoked")
    assert seen["safety_override"] is True
    assert seen["override_reason"] == "reviewed by hand; the key is revoked"


def test_promotion_never_sets_the_override_on_its_own():
    seen: dict = {}

    def transport(method, url, payload):
        seen.update(payload or {})
        return {}

    WikiBrainMemoryAdapter(transport=transport).promote_candidate("c", "matthew")
    assert "safety_override" not in seen


def test_capture_without_an_origin_actor_is_refused_by_name():
    """AC-5: it used to die inside BrainConnect with a `TypeError` about `proposed_by`.

    No default actor is invented. A memory claim's provenance is not ours to guess.
    """
    called = []

    def transport(method, url, payload):
        called.append(url)
        return {}

    a = WikiBrainMemoryAdapter(transport=transport)
    with pytest.raises(InvalidRequest, match="origin_actor_id"):
        a.capture_candidate(CaptureRequest(text="x"))
    with pytest.raises(InvalidRequest, match="origin_actor_id"):
        a.capture_candidate(CaptureRequest(text="x", origin_actor_id="  "))
    assert called == [], "a doomed capture must not reach the backend"
