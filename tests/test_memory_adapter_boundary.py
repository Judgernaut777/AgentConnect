"""The WikiBrain adapter owns the wire: vocabulary mapping and error classification.

Two release-verification defects pinned here:

**A1 — actor vocabulary.** The ledger's actor vocabulary is BrainConnect trust
semantics (``human | manager | worker | librarian | agent | tool``) and it 400s on
anything else. AgentConnect's vocabulary has ``system`` (CaptureRequest,
``ac memory capture --actor-type``). The consumer adapts: the *wire adapter* maps
``"system" -> "tool"`` when talking to the ledger — an automated non-agent system
action is a tool action in ledger terms — and sends every other value verbatim.
The mapping is the adapter's, never global: the CaptureRequest itself must not be
rewritten, because other backends receive what the caller said.

**A2 — error classification everywhere.** ``_classified`` used to guard only
capture and promotion; recall, ``list_pending`` and ``record_feedback`` leaked raw
``httpx.HTTPStatusError``, so a 403 in bearer-token mode never became
``MemoryAuthorizationError``. Every adapter HTTP call now classifies its failure.
"""

from __future__ import annotations

import pytest

from agentconnect.core.errors import InvalidRequest
from agentconnect.core.memory import (
    CaptureRequest,
    MemoryAuthorizationError,
    MemoryFeedbackRequest,
    MemoryServerError,
    RecallRequest,
    WikiBrainMemoryAdapter,
)

# ------------------------------------------------------- A1: actor vocabulary


def _capturing_adapter(seen: dict):
    def transport(method, url, payload):
        seen["method"] = method
        seen["url"] = url
        seen["payload"] = payload
        return {"accepted": True, "candidate_id": "cand_1", "status": "pending"}
    return WikiBrainMemoryAdapter(transport=transport)


def test_system_actor_type_crosses_the_wire_as_tool():
    """A1: the ledger has no "system"; the adapter sends "tool" and the capture works."""
    seen: dict = {}
    result = _capturing_adapter(seen).capture_candidate(CaptureRequest(
        text="the reaper thread is opt-in", origin_actor_id="router",
        origin_actor_type="system"))

    assert seen["url"].endswith("/capture")
    assert seen["payload"]["origin_actor_type"] == "tool"
    assert result.accepted is True
    assert result.candidate_id == "cand_1"


def test_the_mapping_never_rewrites_the_request_itself():
    """Other backends must keep seeing what the caller said: the wire adapter maps,
    the CaptureRequest is not mutated."""
    request = CaptureRequest(text="x", origin_actor_id="router",
                             origin_actor_type="system")
    _capturing_adapter({}).capture_candidate(request)
    assert request.origin_actor_type == "system"


@pytest.mark.parametrize("actor_type", [
    "human", "manager", "worker", "librarian", "agent", "tool", None,
])
def test_every_other_actor_type_crosses_verbatim(actor_type):
    seen: dict = {}
    _capturing_adapter(seen).capture_candidate(CaptureRequest(
        text="x", origin_actor_id="claude", origin_actor_type=actor_type))
    assert seen["payload"]["origin_actor_type"] == actor_type


# --------------------------------------------- A2: classification everywhere


class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _HttpStatusError(Exception):
    """Structurally what httpx raises; recognized by shape, not by import."""

    def __init__(self, response):
        super().__init__(f"http error {response.status_code}")
        self.response = response


def _raising(status, body=None):
    def transport(method, url, payload):
        raise _HttpStatusError(_Response(status, body))
    return WikiBrainMemoryAdapter(transport=transport)


RECALL = RecallRequest(query="tokens", profile="manager_brief")
FEEDBACK = MemoryFeedbackRequest(task_id="t1", memory_item_id="claim_1",
                                 source_id=None, feedback="stale", actor_id="m1")


def test_a_403_on_recall_is_an_authorization_error_not_a_raw_http_exception():
    with pytest.raises(MemoryAuthorizationError):
        _raising(403).recall(RECALL)


def test_a_400_on_recall_is_an_invalid_request():
    with pytest.raises(InvalidRequest):
        _raising(400).recall(RECALL)


def test_a_403_on_feedback_is_an_authorization_error():
    with pytest.raises(MemoryAuthorizationError):
        _raising(403).record_feedback(FEEDBACK)


def test_a_400_on_feedback_is_an_invalid_request():
    with pytest.raises(InvalidRequest):
        _raising(400).record_feedback(FEEDBACK)


def test_a_403_on_list_pending_is_an_authorization_error():
    with pytest.raises(MemoryAuthorizationError):
        _raising(403).list_pending()


def test_a_forbidden_envelope_on_recall_names_the_actor_problem():
    """The envelope code is authoritative when present, on reads as on writes."""
    body = {"error": {"code": "forbidden", "message": "this token may not recall"}}
    with pytest.raises(MemoryAuthorizationError, match="may not recall"):
        _raising(403, body).recall(RECALL)


def test_a_500_on_recall_is_a_server_error():
    with pytest.raises(MemoryServerError):
        _raising(500).recall(RECALL)


def test_the_recall_success_path_is_unchanged():
    """Classification wraps the failure path only; a good answer parses as before."""
    pack = WikiBrainMemoryAdapter(transport=lambda m, u, p: {
        "items": [{"id": "claim_1", "text": "fact", "status": "promoted",
                   "confidence": "high", "trusted": True}],
        "warnings": [],
    }).recall(RECALL)
    assert [i.text for i in pack.items] == ["fact"]
    assert pack.items[0].metadata["trusted"] is True
