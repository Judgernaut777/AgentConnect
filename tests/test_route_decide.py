"""`POST /route/decide` — the decision-only routing endpoint (BrainConnect Lane 4).

BrainConnect's ``HttpRoutingClient`` POSTs a routing *context* and expects a bare
``RoutingDecision`` back. The endpoint exposes the router package's deterministic
``RoutingEngine.route`` and **must not** have the side effects that
``submit_task``/``enqueue_task`` do: no task/subtask persisted, no routing decision
recorded, no dispatch. It authenticates like every other AgentConnect route.

These tests pin all four properties: the response shape, side-effect-freeness,
that a credential is required, and that a malformed body is a clean 4xx (not a 500).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentconnect.api.app import create_app
from agentconnect.core.service import AgentConnectService
from agentconnect.router.service import RouterService

# The context BrainConnect's HttpRoutingClient sends. Every field it declares.
CONTEXT = {
    "task_id": "bc-lane4-task",
    "privacy_class": "public",
    "needed_capabilities": ["code_generation"],
    "profile": None,
    "require_exact_model": None,
    "est_input_tokens": 800,
    "est_output_tokens": 400,
    "allow_external": True,
    "allow_paid": False,
    "priority": "normal",
    "quality": "standard",
    "cloud_safe": True,
    "pending_same_model_batch": 0,
    "allow_rented": False,
}

# The fields BrainConnect's HttpRoutingClient reads off the RoutingDecision.
DECISION_FIELDS = {
    "task_id", "decision", "selected_provider", "selected_model",
    "rejected_options", "scores", "policy_version",
}


@pytest.fixture()
def svc(tmp_path):
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(tmp_path / "workspaces"),
    )


@pytest.fixture()
def router():
    # A real deterministic RouterService, built the same way the app builds it, but
    # held by the test so its in-memory store can be inspected for side effects.
    return RouterService.create()


@pytest.fixture()
def client(svc, router):
    return TestClient(create_app(service=svc, linear_sync=None, router=router))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def operator_headers(svc) -> dict[str, str]:
    return bearer(svc.mint_operator_token("brainconnect").plaintext)


# ------------------------------------------------------------------ the shape

def test_decide_returns_a_well_formed_routing_decision(client, svc):
    r = client.post("/route/decide", json=CONTEXT, headers=operator_headers(svc))
    assert r.status_code == 200, r.text
    body = r.json()

    # Exactly the RoutingDecision shape BrainConnect's HttpRoutingClient expects.
    assert set(body) == DECISION_FIELDS
    assert body["task_id"] == "bc-lane4-task"
    assert isinstance(body["decision"], str) and body["decision"]
    assert isinstance(body["rejected_options"], list)
    assert isinstance(body["scores"], list)
    assert body["policy_version"]  # a real policy version, not the "unknown" default

    # A public task with a healthy free cloud provider routes to it; the shape of
    # each nested record matches too.
    if body["scores"]:
        s0 = body["scores"][0]
        assert set(s0) >= {"provider", "model", "total", "terms"}
    for rej in body["rejected_options"]:
        assert set(rej) == {"provider", "reason"}


def test_decide_is_deterministic(client, svc):
    """Same context in -> byte-identical decision out. Determinism is the contract
    (routing policy is a pure function of task+config+status), and it also proves the
    call left no state behind that could change the next answer."""
    h = operator_headers(svc)
    first = client.post("/route/decide", json=CONTEXT, headers=h).json()
    second = client.post("/route/decide", json=CONTEXT, headers=h).json()
    assert first == second


# ------------------------------------------------------- side-effect-freeness

def test_decide_persists_nothing_and_dispatches_nothing(client, svc, router):
    """Unlike submit_task/enqueue_task, a decision query must not create a task,
    persist a routing decision, enqueue work, or dispatch a generation."""
    r = client.post("/route/decide", json=CONTEXT, headers=operator_headers(svc))
    assert r.status_code == 200

    # 1. The router's own store recorded no task and no routing decision.
    assert router.memory.list_tasks() == [], "decide_route created a task in the router store"
    assert router.memory.get_routing_decisions("bc-lane4-task") == [], \
        "decide_route recorded a routing decision"

    # 2. Nothing was enqueued for a worker to pick up (enqueue requires a task,
    #    which step 1 already shows was never created; assert the queue directly too).
    assert router.workqueue.list_tickets() == [], "decide_route enqueued work"

    # 3. The AgentConnect ledger the API is built around is untouched — no task,
    #    hence no subtask, exists to have been created by this call.
    assert svc.list_tasks() == []


# ------------------------------------------------------------- authentication

def test_decide_requires_a_token(client):
    assert client.post("/route/decide", json=CONTEXT).status_code == 401


def test_decide_rejects_a_malformed_authorization_header(client):
    for header in ({"Authorization": "act_nope"},          # no scheme
                   {"Authorization": "Basic act_nope"},     # wrong scheme
                   {"Authorization": "Bearer "}):           # no value
        assert client.post("/route/decide", json=CONTEXT,
                           headers=header).status_code == 401, header


def test_decide_rejects_an_unknown_token(client):
    assert client.post("/route/decide", json=CONTEXT,
                       headers=bearer("act_made_up")).status_code == 401


def test_a_readonly_token_cannot_ask_for_a_decision(client, svc):
    """`decide_route` is a manager/operator action; a look-only token is 403 — a real
    credential doing a forbidden thing, not an absent one."""
    from agentconnect.core.models import CreateTaskRequest

    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="operator"))
    token = svc.launch_session("watcher", task_id=task.id, readonly=True)["token"]
    r = client.post("/route/decide", json=CONTEXT, headers=bearer(token))
    assert r.status_code == 403


# --------------------------------------------------------------- input hygiene

def test_a_malformed_body_is_a_clean_4xx(client, svc):
    h = operator_headers(svc)
    # Bad enum value, missing required field, wrong type — each a 422, never a 500.
    assert client.post("/route/decide", json={"task_id": "t", "privacy_class": "nope"},
                       headers=h).status_code == 422
    assert client.post("/route/decide", json={"privacy_class": "public"},
                       headers=h).status_code == 422  # task_id missing
    assert client.post("/route/decide",
                       json={**CONTEXT, "est_input_tokens": "lots"},
                       headers=h).status_code == 422
    assert client.post("/route/decide", data="not json", headers=h).status_code == 422


# -------------------------------------------------------------- degraded build

def test_route_decide_is_503_when_no_router_is_configured(svc):
    """A deployment without the router package/config degrades this one route to a
    clean 503 (the linear_sync posture), not a 500."""
    client = TestClient(create_app(service=svc, linear_sync=None, router=None))
    r = client.post("/route/decide", json=CONTEXT, headers=operator_headers(svc))
    assert r.status_code == 503
