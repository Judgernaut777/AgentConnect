"""The HTTP adapter authenticates, and the token is the authority.

Before this suite existed, `POST /tasks/{id}/complete` with `force: true` was
reachable by anyone who could open a socket — and a managed agent was *handed* the
address in `AGENTCONNECT_API_HOST` / `AGENTCONNECT_API_PORT`. An agent could mark its
own task `succeeded`, attributed to any name it typed, without the audit ever running.

Two of the five rules in the operational contract died there. The last test in this
file runs a real HTTP server on a real port and replays that exact bypass.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from conftest import operator_client  # noqa: E402
from fastapi.testclient import TestClient

from agentconnect.api.app import (
    create_app,
    declared_routes,
    phantom_routes,
    unmapped_routes,
)
from agentconnect.core.models import (
    ArtifactType,
    CreateArtifactRequest,
    CreateTaskRequest,
    ReviewRequest,
)
from agentconnect.core.service import AgentConnectService


@pytest.fixture()
def svc(tmp_path):
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(tmp_path / "workspaces"),
    )


@pytest.fixture()
def task(svc):
    return svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="operator"))


@pytest.fixture()
def anon(svc):
    """A client with no credential at all."""
    return TestClient(create_app(service=svc, linear_sync=None))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def manager_token(svc, task_id: str) -> str:
    return svc.launch_session("claude", task_id=task_id, claim=True)["token"]


def reviewer_token(svc, task_id: str) -> str:
    artifact = svc.create_artifact(task_id, CreateArtifactRequest(
        type=ArtifactType.report, content="x", summary="s", created_by="claude"))
    review = svc.request_review(task_id, ReviewRequest(
        requested_by="claude", assigned_to="codex", artifact_refs=[artifact.id]))
    return svc.launch_session("codex", review_id=review.id)["token"]


# ------------------------------------------------------------------ the surface

def test_every_route_declares_an_action_and_no_declared_route_is_a_phantom(svc):
    """The check that would have caught `.mcp.json`'s `get_subtask_status`."""
    app = create_app(service=svc, linear_sync=None)
    assert len(declared_routes(app)) > 10, "the route walk found nothing; it is broken"
    assert unmapped_routes(app) == []
    assert phantom_routes(app) == []


def test_only_liveness_and_readiness_probes_serve_without_a_token(anon, svc, task):
    # Liveness and readiness are the only unauthenticated routes: both are
    # infrastructure probes that return no ledger contents.
    assert anon.get("/health").status_code == 200
    assert anon.get("/ready").status_code in (200, 503)
    for method, path in declared_routes(create_app(service=svc, linear_sync=None)):
        if (method, path) in (("GET", "/health"), ("GET", "/ready"),
                              ("POST", "/linear/webhook")):
            continue
        concrete = (path.replace("{task_id}", task.id)
                        .replace("{review_id}", "review_x")
                        .replace("{subtask_id}", "subtask_x")
                        .replace("{artifact_id}", "artifact_x")
                        .replace("{workflow_id}", "wf_x")
                        .replace("{session_id}", "session_x")
                        .replace("{workspace_id}", "workspace_x")
                        .replace("{manager_id}", "claude"))
        response = anon.request(method, concrete, json={})
        assert response.status_code == 401, f"{method} {concrete} served without a token"


# ------------------------------------------------------------ the credential

def test_a_missing_token_is_401(anon, task):
    assert anon.post(f"/tasks/{task.id}/complete", json={}).status_code == 401


def test_a_malformed_authorization_header_is_401(anon, task):
    for header in ({"Authorization": "act_nope"},           # no scheme
                   {"Authorization": "Basic act_nope"},     # wrong scheme
                   {"Authorization": "Bearer "}):           # no value
        response = anon.post(f"/tasks/{task.id}/complete", json={}, headers=header)
        assert response.status_code == 401, header


def test_an_unknown_token_is_401(anon, task):
    response = anon.post(f"/tasks/{task.id}/attempts", json={}, headers=bearer("act_made_up"))
    assert response.status_code == 401


def test_a_revoked_token_is_401(anon, svc, task):
    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    token = launched["token"]
    assert anon.get(f"/tasks/{task.id}", headers=bearer(token)).status_code == 200

    svc.end_shell(launched["session"].id, 0)   # the shell exits
    response = anon.get(f"/tasks/{task.id}", headers=bearer(token))
    assert response.status_code == 401, "a leaked .env.agentconnect must be inert"


def test_shell_exit_revokes_the_token_for_every_transport(anon, svc, task):
    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    token = launched["token"]
    svc.end_shell(launched["session"].id, 0)

    assert anon.post(f"/tasks/{task.id}/attempts", json={
        "actor_id": "claude", "summary": "late"}, headers=bearer(token)).status_code == 401


# ---------------------------------------------------------------------- scope

def test_a_token_scoped_to_one_task_cannot_touch_another(anon, svc, task):
    other = svc.create_task(CreateTaskRequest(title="other", goal="g", created_by="operator"))
    token = manager_token(svc, task.id)

    assert anon.get(f"/tasks/{task.id}", headers=bearer(token)).status_code == 200
    response = anon.get(f"/tasks/{other.id}", headers=bearer(token))
    assert response.status_code == 403
    assert "scoped to task_id" in str(response.json())


def test_a_manager_token_records_the_evidence_it_is_meant_to(anon, svc, task):
    token = manager_token(svc, task.id)
    assert anon.post(f"/tasks/{task.id}/attempts", json={
        "actor_id": "claude", "summary": "did the work"},
        headers=bearer(token)).status_code == 201
    assert anon.post(f"/tasks/{task.id}/decisions", json={
        "made_by": "claude", "decision": "use approach A"},
        headers=bearer(token)).status_code == 201


# ----------------------------------------------------------------- completion

def test_a_manager_token_cannot_complete_its_own_task(anon, svc, task):
    response = anon.post(f"/tasks/{task.id}/complete", json={},
                         headers=bearer(manager_token(svc, task.id)))
    assert response.status_code == 403
    assert "complete_task" in str(response.json())


def test_a_reviewer_token_cannot_complete_the_task(anon, svc, task):
    response = anon.post(f"/tasks/{task.id}/complete", json={},
                         headers=bearer(reviewer_token(svc, task.id)))
    assert response.status_code == 403


def test_no_managed_token_can_reach_the_override(anon, svc, task):
    """`force` is not a field any more. It is an endpoint, and it is operator-only."""
    for token in (manager_token(svc, task.id), reviewer_token(svc, task.id)):
        response = anon.post(f"/tasks/{task.id}/complete/override",
                             json={"reason": "because I said so"}, headers=bearer(token))
        assert response.status_code == 403


def test_force_is_not_accepted_by_the_ordinary_completion_route(anon, svc, task):
    """Sending `force: true` must not skip the audit. It is ignored, and the audit runs."""
    token = svc.mint_operator_token("matthew").plaintext
    response = anon.post(f"/tasks/{task.id}/complete", json={"force": True},
                         headers=bearer(token))
    assert response.status_code == 403
    assert "audit failed" in str(response.json())


def test_an_operator_completes_only_after_the_audit_passes(anon, svc, tmp_path):
    token = svc.mint_operator_token("matthew").plaintext
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="operator"))

    refused = anon.post(f"/tasks/{task.id}/complete", json={}, headers=bearer(token))
    assert refused.status_code == 403 and "audit failed" in str(refused.json())

    accepted = anon.post(f"/tasks/{task.id}/complete/override",
                         json={"reason": "dogfood run; work verified by hand"},
                         headers=bearer(token))
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "succeeded"
    assert body["override"]["authorized_by"] == "matthew"
    assert body["override"]["audit_passed"] is False
    assert body["override"]["failed_checks"]


def test_an_override_without_a_reason_is_refused(anon, svc, task):
    token = svc.mint_operator_token("matthew").plaintext
    assert anon.post(f"/tasks/{task.id}/complete/override", json={"reason": "   "},
                     headers=bearer(token)).status_code == 400
    assert anon.post(f"/tasks/{task.id}/complete/override", json={},
                     headers=bearer(token)).status_code == 422  # reason is required


def test_the_override_is_recorded_in_the_ledger_before_the_task_is_touched(anon, svc, task):
    token = svc.mint_operator_token("matthew").plaintext
    anon.post(f"/tasks/{task.id}/complete/override",
              json={"reason": "release cut; audit waived deliberately"},
              headers=bearer(token))

    decisions = svc.list_decisions(task.id)
    override = [d for d in decisions if "override" in d.decision.lower()]
    assert override, "an administrative override left no trace in the ledger"
    assert override[0].made_by == "matthew"
    assert override[0].locked is True
    assert "audit waived deliberately" in override[0].rationale


# ------------------------------------------------------------- impersonation

def test_the_body_cannot_name_a_different_actor_than_the_token(anon, svc, task):
    token = manager_token(svc, task.id)
    response = anon.post(f"/tasks/{task.id}/attempts", json={
        "actor_id": "someone-else", "summary": "not me"}, headers=bearer(token))
    assert response.status_code == 403
    assert "cannot act as" in str(response.json())


def test_completion_is_attributed_to_the_token_not_the_body(anon, svc, task):
    """`completed_by` is gone from the schema; the principal is the record."""
    token = svc.mint_operator_token("matthew").plaintext
    anon.post(f"/tasks/{task.id}/complete/override",
              json={"reason": "r", "completed_by": "somebody-important"},
              headers=bearer(token))
    assert svc.list_decisions(task.id)[0].made_by == "matthew"


def test_an_operator_may_act_for_the_agents_it_launches(anon, svc, task):
    """The control plane names other actors. That is what a control plane does."""
    token = svc.mint_operator_token("matthew").plaintext
    assert anon.post(f"/tasks/{task.id}/claim", json={
        "manager_id": "claude"}, headers=bearer(token)).status_code == 201


# ------------------------------------------------- the bypass, over real HTTP

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_managed_agent_cannot_complete_its_task_over_a_real_http_server(svc, task):
    """The original AC-1 bypass, replayed against a real socket.

    A managed session holds `AGENTCONNECT_SESSION_TOKEN` and is told the API's host
    and port. It knocks. Every door it used to walk through is now shut.
    """
    import uvicorn

    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    agent_token = launched["token"]

    port = _free_port()
    config = uvicorn.Config(create_app(service=svc, linear_sync=None),
                            host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "the test server never came up"

        import httpx

        base = f"http://127.0.0.1:{port}"
        agent = {"Authorization": f"Bearer {agent_token}"}

        # It is a real server, and the agent's token really works for its own job.
        assert httpx.post(f"{base}/tasks/{task.id}/attempts",
                          json={"actor_id": "claude", "summary": "worked"},
                          headers=agent).status_code == 201

        # 1. No credential at all — the original hole.
        assert httpx.post(f"{base}/tasks/{task.id}/complete",
                          json={"completed_by": "me", "force": True}).status_code == 401

        # 2. The agent's own token, ordinary completion.
        assert httpx.post(f"{base}/tasks/{task.id}/complete", json={},
                          headers=agent).status_code == 403

        # 3. The agent's own token, reaching for `force` the way it used to.
        assert httpx.post(f"{base}/tasks/{task.id}/complete",
                          json={"force": True}, headers=agent).status_code == 403

        # 4. The agent's own token, against the override endpoint.
        assert httpx.post(f"{base}/tasks/{task.id}/complete/override",
                          json={"reason": "trust me"}, headers=agent).status_code == 403

        # 5. And promotion, which is how an agent would launder its own output.
        assert httpx.post(f"{base}/memory/promote",
                          json={"candidate_id": "c1", "promoted_by": "claude"},
                          headers=agent).status_code == 403

        assert svc.get_task(task.id).task.status.value != "succeeded"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
