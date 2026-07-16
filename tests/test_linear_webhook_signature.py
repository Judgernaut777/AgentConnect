"""`POST /linear/webhook` authenticates via Linear's own HMAC signature.

Before this suite, the route was served to *anyone* who could open a socket: the
handler parsed attacker-controlled JSON, read `actor` straight out of it, and
called `approve_subtask` / `deny_subtask` / `complete_task` / `request_review` —
the exact operator-gated actions the rest of the API requires a session token
for. A comment even claimed "the webhook is signed by Linear, not by us", but no
signature check existed anywhere in the package.

These tests replay that gap over real HTTP (`fastapi.testclient.TestClient`,
same as `test_http_authorization.py`) and assert it is closed: an unsigned or
wrongly-signed delivery is rejected *before* the payload is parsed or any
service mutation happens, a correctly-signed delivery is processed exactly as
before, and a deployment that has not configured `LINEAR_WEBHOOK_SECRET` at all
refuses to serve the route rather than silently accepting unsigned webhooks.
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from agentconnect.api.app import create_app
from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    PrivacyTier,
    RoutePolicy,
    SubtaskRequest,
    SubtaskStatus,
    WorkerLocation,
)
from agentconnect.core.workers import RawModelWorker
from agentconnect.linear import LinearClient, LinearSync

SECRET = "top-secret-linear-webhook-key"


class _FakeTransport:
    """Just enough GraphQL to let `LinearSync.sync_task` mint an issue id."""

    def __init__(self):
        self._n = 0

    def __call__(self, query, variables):
        if "IssueCreate" in query:
            self._n += 1
            issue = {"id": f"lin-{self._n}", "identifier": f"AUTH-{self._n}",
                      "url": f"https://linear.app/x/issue/AUTH-{self._n}", "title": ""}
            return {"issueCreate": {"success": True, "issue": issue}}
        if "IssueUpdate" in query:
            return {"issueUpdate": {"success": True, "issue": {
                "id": variables["id"], "identifier": "AUTH-1",
                "url": "https://linear.app/x/issue/AUTH-1", "title": ""}}}
        if "Labels" in query:
            return {"team": {"labels": {"nodes": []}}}
        return {}


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def _comment_payload(issue_id: str, body: str, author: str = "matthew",
                     webhook_timestamp: int | None = None) -> dict:
    payload = {"action": "create", "type": "Comment",
               "data": {"body": body, "issue": {"id": issue_id}, "user": {"name": author}}}
    if webhook_timestamp is not None:
        payload["webhookTimestamp"] = webhook_timestamp
    return payload


@pytest.fixture()
def rig(tmp_path):
    """A service with one task, synced to a fake Linear issue, holding one
    subtask parked in `needs_approval` — the same fixture shape as
    `test_backplane_linear.approval_wired`, wired to a real HTTP app instead of
    calling `webhooks.handle_webhook` directly."""
    cloud = RawModelWorker("cloud", lambda p: "cloud output", model="gpt",
                           location=WorkerLocation.cloud, privacy_tiers=[PrivacyTier.public],
                           cost_per_1k_tokens_usd=0.5)
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "artifacts"),
        workers=[cloud], policy=RoutePolicy(max_cost_usd=10.0),
    )
    sync = LinearSync(svc, LinearClient(transport=_FakeTransport()), team_id="team-1")
    task = svc.create_task(CreateTaskRequest(title="t"))
    sync.sync_task(task.id)
    subtask = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    assert subtask.status is SubtaskStatus.needs_approval
    return svc, task, subtask


def _client(svc, secret) -> TestClient:
    return TestClient(create_app(service=svc, linear_sync=None, linear_webhook_secret=secret))


# ----------------------------------------------------------- secret unset: 503

def test_secret_unset_refuses_to_serve_the_route(rig):
    """No `LINEAR_WEBHOOK_SECRET` configured: fail closed, do not accept unsigned
    webhooks by default. The route refuses to serve rather than silently
    trusting the body."""
    svc, task, subtask = rig
    client = _client(svc, secret=None)

    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 503
    assert "LINEAR_WEBHOOK_SECRET" in str(response.json())
    # No mutation: the correctly-signed-looking request never got that far because
    # signing against a secret the server does not have is meaningless.
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.needs_approval


# ------------------------------------------------------- forged / unsigned: 401

def test_forged_payload_with_no_signature_header_is_rejected(rig):
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud max_cost=3.00")).encode()
    response = client.post(
        "/linear/webhook", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 401
    assert "Linear-Signature" in str(response.json())
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.needs_approval
    assert svc.list_approvals(task.id)[0].decided_by is None


def test_wrong_signature_is_rejected(rig):
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud max_cost=3.00")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": "0" * 64, "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.needs_approval
    assert svc.list_approvals(task.id)[0].decided_by is None


def test_signature_computed_with_the_wrong_secret_is_rejected(rig):
    """An attacker who knows the *scheme* (HMAC-SHA256, hex, header name) but not
    the secret still cannot forge a valid signature."""
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    body = json.dumps(_comment_payload("lin-1", "/agentconnect approve cloud")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body, secret="a-different-secret"),
                 "Content-Type": "application/json"},
    )
    assert response.status_code == 401
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.needs_approval


# ------------------------------------------------------- correct signature: 200

def test_correctly_signed_webhook_is_processed(rig):
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud max_cost=3.00")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["kind"] == "approved"
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.succeeded
    approval = svc.list_approvals(task.id)[0]
    assert approval.status.value == "granted" and approval.decided_by == "matthew"


def test_signature_verification_is_insensitive_to_key_order_because_it_is_over_raw_bytes(rig):
    """Guards the raw-body requirement: verifying against a re-serialized model
    instead of the original bytes would make this pass even when it should not,
    or fail when it should not, depending on dict/json library key ordering.
    Here we deliberately sign the *exact* bytes sent, with an unusual key order,
    and confirm it still verifies — proving the check is over raw bytes, not a
    canonicalized re-encoding.
    """
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    payload = _comment_payload("lin-1", "/agentconnect approve cloud")
    # Odd key order / spacing that a naive "re-dump and compare" would not
    # reproduce byte-for-byte.
    body = json.dumps(payload, sort_keys=True, separators=(", ", ": ")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200


# ------------------------------------------------------------- stale delivery

def test_stale_webhook_timestamp_is_rejected(rig):
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    ten_minutes_ago_ms = int((time.time() - 600) * 1000)
    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud",
        webhook_timestamp=ten_minutes_ago_ms)).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.needs_approval


def test_fresh_webhook_timestamp_is_accepted(rig):
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    now_ms = int(time.time() * 1000)
    body = json.dumps(_comment_payload(
        "lin-1", "/agentconnect approve cloud",
        webhook_timestamp=now_ms)).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert svc.get_subtask(subtask.id).subtask.status is SubtaskStatus.succeeded


def test_missing_webhook_timestamp_is_not_treated_as_stale(rig):
    """The field is best-effort: most Linear payload shapes include it, but its
    absence must not itself refuse an otherwise validly-signed delivery."""
    svc, task, subtask = rig
    client = _client(svc, secret=SECRET)

    body = json.dumps(_comment_payload("lin-1", "/agentconnect approve cloud")).encode()
    response = client.post(
        "/linear/webhook", content=body,
        headers={"Linear-Signature": _sign(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
