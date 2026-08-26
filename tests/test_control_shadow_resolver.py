"""The ledger-backed resolver (ADR 0010, docs/CONTROL_SHADOW.md).

Two things are worth testing here beyond the happy path: that every way a
lookup can fail becomes a skip rather than an exception, and that the resolver
reads the *routing decision* rather than the bus payload — the distinction that
kept it correct while `compute.placed` was reporting a constant.
"""

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    PrivacyTier,
    RawModelWorker,
    RoutePolicy,
    SubtaskRequest,
    WorkerLocation,
)
from agentconnect.core.control_projection import Agreement, ControlPrivacy
from agentconnect.core.control_shadow import (
    PLACED_EVENT_TYPE,
    MemoryShadowSink,
    ShadowConsumer,
    evaluate_once,
    summarize,
)
from agentconnect.core.control_shadow_resolver import LedgerRoutingFactsResolver


def envelope(route_class="inference_box_general"):
    return {
        "schema_version": 1,
        "mode": "ROUTE",
        "decision": {"route_class": route_class, "review_class": "none", "parallelism": 1},
        "confidence": 0.9,
        "reason_codes": ["local_capable"],
        "detected_constraints": [],
        "escalate": False,
    }


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def decide(self, mode, state, repair=""):
        self.calls.append({"mode": mode, "state": dict(state), "repair": repair})
        return self.replies.pop(0) if self.replies else envelope()


def cloud_worker():
    return RawModelWorker(
        "cloudy", lambda p: "out", model="deepseek-v3", location=WorkerLocation.cloud,
        privacy_tiers=[PrivacyTier.public, PrivacyTier.public_redacted],
        capability_tags=["generate"], cost_per_1k_tokens_usd=1.0,
    )


def local_worker():
    return RawModelWorker(
        "localy", lambda p: "out", model="qwen2.5-coder-14b",
        location=WorkerLocation.local, privacy_tiers=list(PrivacyTier),
        capability_tags=["generate", "inspect"], cost_per_1k_tokens_usd=0.0,
    )


@pytest.fixture
def local_route(tmp_path):
    """A real routed subtask on a local worker, plus its placement event."""
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[local_worker()],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="me"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="s", instructions="a prompt that must never reach the model",
        privacy_tier=PrivacyTier.repo_sensitive, required_capabilities=["inspect"],
    ))
    placed = [e for e in svc.storage.list_bus_events(since=0, limit=500)
              if e["type"] == PLACED_EVENT_TYPE]
    return svc, sub, placed[-1]


@pytest.fixture
def cloud_route(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[cloud_worker()],
        policy=RoutePolicy(max_cost_usd=100.0),
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="me"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="s", instructions="summarize", privacy_tier=PrivacyTier.public,
        required_capabilities=["generate"],
    ))
    svc.approve_subtask(sub.id, approved_by="me", max_cost_usd=50.0)
    placed = [e for e in svc.storage.list_bus_events(since=0, limit=500)
              if e["type"] == PLACED_EVENT_TYPE]
    return svc, sub, placed[-1]


# --------------------------------------------------------------------------- #
# It resolves a real routing off a real ledger
# --------------------------------------------------------------------------- #
def test_resolves_a_local_route(local_route):
    svc, sub, event = local_route
    resolved = LedgerRoutingFactsResolver(svc.storage).resolve(event)
    assert resolved is not None
    assert resolved.subtask_id == sub.id
    assert resolved.router.provider_tier == "local_only"
    assert resolved.router.selected_provider == "localy"
    assert resolved.state.privacy is ControlPrivacy.local_only  # repo_sensitive rounds down


def test_resolves_a_cloud_route(cloud_route):
    svc, sub, event = cloud_route
    resolved = LedgerRoutingFactsResolver(svc.storage).resolve(event)
    assert resolved.router.provider_tier == "external"
    assert resolved.state.privacy is ControlPrivacy.public
    assert resolved.state.allow_paid is True, "an approved spend is a recorded fact"


def test_the_prompt_never_reaches_the_model(local_route):
    """`instructions` is the only free text on a Subtask. The resolver must not
    pick it up — the downstream key guard is a backstop, not the plan."""
    svc, _, event = local_route
    resolved = LedgerRoutingFactsResolver(svc.storage).resolve(event)
    client = FakeClient()
    evaluate_once(resolved, client)
    sent = client.calls[0]["state"]
    assert "must never reach the model" not in str(sent)
    assert "instructions" not in sent


# --------------------------------------------------------------------------- #
# The ledger is the authority, not the payload
# --------------------------------------------------------------------------- #
def test_the_payload_location_is_not_what_is_read(cloud_route):
    """A payload claiming the wrong place must not move the answer: the resolver
    reads the RouteExplanation the router itself wrote."""
    svc, _, event = cloud_route
    lying = dict(event, payload={"location": "local"})
    resolved = LedgerRoutingFactsResolver(svc.storage).resolve(lying)
    assert resolved.router.provider_tier == "external", (
        "the resolver followed the bus payload instead of the ledger"
    )


def test_the_recorded_route_carries_the_selected_location(cloud_route):
    svc, sub, _ = cloud_route
    assert svc.explain_route(sub.id).selected_location == "cloud"


# --------------------------------------------------------------------------- #
# Every failure is a skip
# --------------------------------------------------------------------------- #
def test_an_event_with_no_subtask_id_is_skipped(local_route):
    svc, _, event = local_route
    assert LedgerRoutingFactsResolver(svc.storage).resolve(dict(event, subtask_id=None)) is None


def test_an_unknown_subtask_is_skipped(local_route):
    svc, _, event = local_route
    resolver = LedgerRoutingFactsResolver(svc.storage)
    assert resolver.resolve(dict(event, subtask_id="subtask_gone")) is None


def test_a_subtask_with_no_recorded_route_is_skipped(local_route):
    svc, _, event = local_route

    class NoRoute:
        id = "subtask_x"
        parent_task_id = "task_x"
        privacy_tier = PrivacyTier.public
        required_capabilities = []
        approved_max_cost_usd = None
        route_reason = {}

    class Storage:
        def get_subtask(self, _):
            return NoRoute()

    assert LedgerRoutingFactsResolver(Storage()).resolve(event) is None


def test_a_malformed_stored_route_is_skipped_not_raised(local_route):
    svc, _, event = local_route

    class Broken:
        id = "subtask_x"
        parent_task_id = "task_x"
        privacy_tier = PrivacyTier.public
        required_capabilities = []
        approved_max_cost_usd = None
        route_reason = {"selected_worker": object()}  # not serializable into the model

    class Storage:
        def get_subtask(self, _):
            return Broken()

    assert LedgerRoutingFactsResolver(Storage()).resolve(event) is None


def test_a_route_that_never_reached_a_worker_is_skipped(local_route):
    svc, _, event = local_route

    class Unrouted:
        id = "subtask_x"
        parent_task_id = "task_x"
        privacy_tier = PrivacyTier.secret_sensitive
        required_capabilities = []
        approved_max_cost_usd = None
        route_reason = {"subtask_id": "subtask_x", "selected_worker": None}

    class Storage:
        def get_subtask(self, _):
            return Unrouted()

    assert LedgerRoutingFactsResolver(Storage()).resolve(event) is None


# --------------------------------------------------------------------------- #
# End to end, against a real ledger
# --------------------------------------------------------------------------- #
def test_a_full_shadow_pass_over_a_real_ledger(local_route):
    svc, _, _ = local_route
    sink = MemoryShadowSink()
    run = ShadowConsumer(
        svc.storage, LedgerRoutingFactsResolver(svc.storage, policy_version="v1"),
        FakeClient(envelope()), sink, event_types=(PLACED_EVENT_TYPE,),
    ).run_once()

    assert run.evaluated == 1 and run.errors == 0
    record = sink.records[0]
    assert record["agreement"] == Agreement.agree.value
    assert record["router"]["provider_tier"] == "local_only"
    assert record["router"]["policy_version"] == "v1"
    assert summarize(sink.records).agreement_rate == 1.0


def test_a_cloud_route_the_model_called_local_is_a_real_disagreement(cloud_route):
    svc, _, _ = cloud_route
    sink = MemoryShadowSink()
    ShadowConsumer(
        svc.storage, LedgerRoutingFactsResolver(svc.storage),
        FakeClient(envelope("inference_box_general")), sink,
        event_types=(PLACED_EVENT_TYPE,),
    ).run_once()
    assert sink.records[-1]["agreement"] == Agreement.disagree.value


def test_a_cloud_route_the_model_called_cloud_agrees(cloud_route):
    svc, _, _ = cloud_route
    sink = MemoryShadowSink()
    ShadowConsumer(
        svc.storage, LedgerRoutingFactsResolver(svc.storage),
        FakeClient(envelope("cloud_general")), sink, event_types=(PLACED_EVENT_TYPE,),
    ).run_once()
    assert sink.records[-1]["agreement"] == Agreement.agree.value
