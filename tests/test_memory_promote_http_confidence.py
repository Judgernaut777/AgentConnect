"""HTTP `/memory/promote` forwards `confidence` + `scope` to the authority.

Wave D defect: the CLI `memory promote` already threaded `--confidence/--scope`
through to `promote_candidate`, but the HTTP route did not. BrainConnect refuses to
guess either (confidence gates profile filters; a guessed scope leaks a repo-local
fact into global recall), so promoting an agent-captured candidate THROUGH
AgentConnect over HTTP failed. This pins the wire path: the values a caller puts on
the request body reach the trusted adapter's `promote_candidate`.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
from conftest import operator_client  # noqa: E402

from agentconnect.core.memory import (
    CaptureRequest,
    CaptureResult,
    RecallPack,
    RecallRequest,
    TrustedMemoryAdapter,
)
from agentconnect.core.service import AgentConnectService


class _RecordingAuthority(TrustedMemoryAdapter):
    """A stub trusted authority that records the args of the last promotion."""

    def __init__(self) -> None:
        self.last_promote: Optional[dict[str, Any]] = None

    @property
    def backend_name(self) -> str:
        return "wikibrain"  # the default trusted_authority resolves to this

    def recall(self, request: RecallRequest) -> RecallPack:  # pragma: no cover
        return RecallPack(profile=request.profile, query=request.query, items=[])

    def capture_candidate(self, request: CaptureRequest) -> CaptureResult:  # pragma: no cover
        return CaptureResult(accepted=True, candidate_id="cand_1", backend=self.backend_name)

    def promote_candidate(self, candidate_id: str, promoted_by: str,
                          confidence: Optional[str] = None,
                          scope: Optional[str] = None,
                          safety_override: bool = False,
                          override_reason: Optional[str] = None) -> dict[str, Any]:
        self.last_promote = {
            "candidate_id": candidate_id, "promoted_by": promoted_by,
            "confidence": confidence, "scope": scope,
        }
        return {"claim_id": candidate_id, "status": "promoted"}


@pytest.fixture()
def authority() -> _RecordingAuthority:
    return _RecordingAuthority()


@pytest.fixture()
def svc(tmp_path, authority) -> AgentConnectService:
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(tmp_path / "workspaces"),
        memory_backends={"wikibrain": authority},
    )


def test_promote_route_forwards_confidence_and_scope(svc, authority):
    client = operator_client(svc)
    resp = client.post("/memory/promote", json={
        "candidate_id": "cand_1", "promoted_by": "operator",
        "confidence": "high", "scope": "repo:mcp-agentconnect",
    })
    assert resp.status_code == 200, resp.text
    assert authority.last_promote is not None, "promotion never reached the adapter"
    assert authority.last_promote["confidence"] == "high"
    assert authority.last_promote["scope"] == "repo:mcp-agentconnect"


def test_promote_route_leaves_them_none_when_omitted(svc, authority):
    """Optional: a backend that can infer them still works with both unset."""
    client = operator_client(svc)
    resp = client.post("/memory/promote", json={
        "candidate_id": "cand_1", "promoted_by": "operator",
    })
    assert resp.status_code == 200, resp.text
    assert authority.last_promote["confidence"] is None
    assert authority.last_promote["scope"] is None
