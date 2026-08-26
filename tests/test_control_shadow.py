"""Control-model shadow mode (ADR 0010 §3, §4).

The tests that matter most are the negative ones. Shadow mode's whole promise
is that it cannot hurt anything — it decides nothing, blocks nothing, leaks
nothing, and raises nothing into a caller. Each of those is asserted here
rather than left to the docstring.
"""

import json
from pathlib import Path

import pytest

from agentconnect.core.control_projection import (
    Agreement,
    ControlCapabilityClass,
    ControlPrivacy,
)
from agentconnect.core.control_shadow import (
    CONTROL_SCHEMA_VERSION,
    MAX_DECISION_TOKENS,
    PLACED_EVENT_TYPE,
    ROUTED_EVENT_TYPE,
    SHADOW_STATE_KEYS,
    TEMPERATURE,
    AgreementReport,
    DecisionInvalid,
    MemoryShadowSink,
    NormalizedState,
    RouterFacts,
    ShadowConsumer,
    ShadowInput,
    evaluate_once,
    parse_decision,
    summarize,
)


# --------------------------------------------------------------------------- #
# Fakes — the suite never touches a model or a network
# --------------------------------------------------------------------------- #
def envelope(route_class="inference_box_general", **over):
    body = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": "ROUTE",
        "decision": {"route_class": route_class, "review_class": "none", "parallelism": 1},
        "confidence": 0.9,
        "reason_codes": ["local_capable"],
        "detected_constraints": [],
        "escalate": False,
    }
    body.update(over)
    return body


class FakeClient:
    """Replays queued replies; records what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def decide(self, mode, state, repair=""):
        self.calls.append({"mode": mode, "state": dict(state), "repair": repair})
        if not self.replies:
            raise AssertionError("client called more times than the test queued replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeBus:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail
        self.queries = []

    def list_bus_events(self, since=0, limit=100, types=None):
        self.queries.append({"since": since, "limit": limit, "types": types})
        if self.fail:
            raise RuntimeError("bus unavailable")
        rows = [e for e in self.events if e["seq"] > since]
        if types:
            rows = [e for e in rows if e["type"] in types]
        return rows[:limit]


class FakeResolver:
    def __init__(self, mapping, raises_on=()):
        self.mapping = mapping
        self.raises_on = set(raises_on)

    def resolve(self, event):
        if event["seq"] in self.raises_on:
            raise RuntimeError("ledger read failed")
        return self.mapping.get(event["seq"])


def routed(seq, subtask="subtask_1", outcome=None):
    return {
        "seq": seq, "type": ROUTED_EVENT_TYPE, "task_id": "task_1",
        "subtask_id": subtask, "outcome": outcome, "payload": {"worker": "w1"},
    }


def shadow_input(tier="local_only", privacy=ControlPrivacy.local_only):
    return ShadowInput(
        state=NormalizedState(privacy=privacy),
        router=RouterFacts(
            decision="route_to_local_resident_model",
            selected_provider="local_box",
            provider_tier=tier,
            policy_version="v7",
        ),
        task_id="task_1",
        subtask_id="subtask_1",
    )


# --------------------------------------------------------------------------- #
# Nothing in the routing path may reach this module
# --------------------------------------------------------------------------- #
#: Modules that ARE shadow mode, and so may name it. Everything else under
#: `packages/` reaching `control_shadow` would be the live path importing a
#: ~5 s model call, which ADR 0010 §4 forbids. Adding a name here is a
#: deliberate, reviewable act — the guard is on the live path, not on shadow
#: mode growing companions.
SHADOW_MODE_MODULES = frozenset(
    {"control_shadow.py", "control_shadow_resolver.py"}
)


def test_no_live_path_module_imports_shadow_mode():
    """ADR 0010 §4: out-of-band, never inline. A ~5 s model call must never end
    up in front of a deterministic answer, so the import itself is forbidden."""
    root = Path(__file__).resolve().parent.parent / "packages"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in SHADOW_MODE_MODULES:
            continue
        if "control_shadow" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"shadow mode reached from the live path: {offenders}"


def test_the_guard_would_catch_a_live_path_import():
    """The guard is only worth having if it can fail. Prove the scan reaches
    real files by running it for a module the live path legitimately does use."""
    root = Path(__file__).resolve().parent.parent / "packages"
    scanned = list(root.rglob("*.py"))
    assert len(scanned) > 100, "the scan found almost nothing — wrong root?"
    users = [p.name for p in scanned if "control_projection" in p.read_text(encoding="utf-8")]
    assert "control_projection.py" in users and "control_shadow.py" in users


def test_sampling_is_off_and_output_is_capped():
    assert TEMPERATURE == 0.0
    assert MAX_DECISION_TOKENS == 192


# --------------------------------------------------------------------------- #
# The normalized state cannot carry free text
# --------------------------------------------------------------------------- #
def test_state_payload_keys_are_bounded():
    payload = NormalizedState(privacy=ControlPrivacy.public).to_payload()
    assert set(payload) == SHADOW_STATE_KEYS


def test_state_carries_no_prompt_or_identifier_fields():
    """The guard with teeth: a task id, a prompt, or a transcript reaching the
    model would be exactly the leak the bus's own payload rule prevents."""
    payload = NormalizedState(privacy=ControlPrivacy.public).to_payload()
    for banned in ("task_id", "subtask_id", "prompt", "text", "payload", "content",
                   "transcript", "profile", "require_exact_model"):
        assert banned not in payload


def test_state_values_are_json_scalars():
    payload = NormalizedState(privacy=ControlPrivacy.secret_involved).to_payload()
    assert json.loads(json.dumps(payload)) == payload


def test_from_routing_context_projects_privacy_and_drops_the_rest():
    class Ctx:
        task_id = "task_secret"
        privacy_class = "repo_sensitive"
        needed_capabilities = ("python",)
        est_input_tokens = 100
        est_output_tokens = 50
        allow_external = False
        allow_paid = False
        allow_rented = True
        priority = "normal"
        quality = "high"
        cloud_safe = False
        pending_same_model_batch = 3
        profile = "coding_specialist"
        require_exact_model = "qwen3-30b-a3b"

    state = NormalizedState.from_routing_context(Ctx())
    # repo_sensitive has no control counterpart: it must round DOWN to stricter.
    assert state.privacy is ControlPrivacy.local_only
    assert state.needed_capabilities == ("python",)
    assert state.quality == "high"
    payload = state.to_payload()
    assert "task_secret" not in json.dumps(payload)
    assert "coding_specialist" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Decision parsing is strict
# --------------------------------------------------------------------------- #
def test_a_valid_envelope_parses():
    d = parse_decision(envelope())
    assert d.route_class is ControlCapabilityClass.inference_box_general
    assert d.confidence == 0.9
    assert d.escalate is False


def test_a_json_string_parses():
    assert parse_decision(json.dumps(envelope())).mode == "ROUTE"


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("not json at all", "not JSON"),
        ([1, 2, 3], "must be an object"),
        (envelope(**{"schema_version": 2}), "schema_version"),
        (envelope(mode="CLASSIFY"), "mode"),
        (envelope(decision="nope"), "decision must be an object"),
        (envelope(route_class="teleportation"), "unknown route_class"),
        (envelope(confidence="high"), "confidence must be a number"),
        (envelope(confidence=1.5), "outside [0,1]"),
        (envelope(confidence=True), "confidence must be a number"),
        (envelope(escalate="yes"), "escalate must be a boolean"),
        (envelope(reason_codes="a"), "must be a list"),
        (envelope(reason_codes=[1]), "must be strings"),
        (envelope(reason_codes=["a"] * 7), "cap is 6"),
        (envelope(detected_constraints=["c"] * 7), "cap is 6"),
    ],
)
def test_malformed_envelopes_are_rejected(raw, fragment):
    with pytest.raises(DecisionInvalid) as exc:
        parse_decision(raw)
    assert fragment in str(exc.value)


def test_escalation_gates_on_the_flag_not_confidence():
    """The calibration report says confidence has low resolution, so a
    threshold on it would be noise wearing a number."""
    low_confidence = parse_decision(envelope(confidence=0.05))
    assert low_confidence.should_escalate is False
    flagged = parse_decision(envelope(confidence=0.99, escalate=True))
    assert flagged.should_escalate is True


# --------------------------------------------------------------------------- #
# evaluate_once: never raises, records everything
# --------------------------------------------------------------------------- #
def test_agreement_is_recorded_when_both_chose_local():
    rec = evaluate_once(shadow_input(), FakeClient(envelope()), seq=7)
    assert rec.agreement is Agreement.agree
    assert rec.model_error is None
    assert rec.seq == 7
    assert rec.router["policy_version"] == "v7"


def test_disagreement_is_recorded():
    rec = evaluate_once(shadow_input(tier="external_paid"), FakeClient(envelope()))
    assert rec.agreement is Agreement.disagree


def test_rented_route_is_unrepresentable_not_a_miss():
    rec = evaluate_once(shadow_input(tier="private_rented"), FakeClient(envelope()))
    assert rec.agreement is Agreement.unrepresentable


def test_an_unreachable_model_is_recorded_not_raised():
    rec = evaluate_once(shadow_input(), FakeClient(ConnectionError("refused")))
    assert rec.model is None
    assert "model call failed" in rec.model_error
    assert rec.agreement is None


def test_a_malformed_reply_gets_exactly_one_repair_attempt():
    client = FakeClient("{bad", envelope())
    rec = evaluate_once(shadow_input(), client)
    assert rec.model_error is None
    assert len(client.calls) == 2
    assert client.calls[0]["repair"] == ""
    assert "not JSON" in client.calls[1]["repair"], "the retry must carry the rejection"


def test_two_malformed_replies_stop_and_record_the_gap():
    client = FakeClient("{bad", "{still bad")
    rec = evaluate_once(shadow_input(), client)
    assert rec.model is None
    assert "invalid decision" in rec.model_error
    assert len(client.calls) == 2, "no third attempt"


def test_an_unknown_provider_tier_does_not_crash_the_comparison():
    bad = ShadowInput(
        state=NormalizedState(privacy=ControlPrivacy.public),
        router=RouterFacts(decision="route_to_cloud_provider", provider_tier="martian"),
    )
    rec = evaluate_once(bad, FakeClient(envelope()))
    assert rec.agreement is Agreement.unrepresentable


def test_a_router_decision_with_no_provider_is_uncompared():
    blocked = ShadowInput(
        state=NormalizedState(privacy=ControlPrivacy.secret_involved),
        router=RouterFacts(decision="blocked_secret_sensitive"),
    )
    rec = evaluate_once(blocked, FakeClient(envelope()))
    assert rec.agreement is None
    assert rec.model is not None


def test_the_record_serializes_to_the_quadruple():
    rec = evaluate_once(shadow_input(), FakeClient(envelope()), seq=3, outcome="succeeded")
    d = rec.to_dict()
    assert set(d) >= {"state", "model", "router", "outcome", "agreement"}
    assert d["outcome"] == "succeeded"
    assert json.loads(json.dumps(d)) == d


# --------------------------------------------------------------------------- #
# The consumer
# --------------------------------------------------------------------------- #
def test_consumer_records_one_shadow_per_routing_event():
    bus = FakeBus([routed(1), routed(2)])
    resolver = FakeResolver({1: shadow_input(), 2: shadow_input()})
    sink = MemoryShadowSink()
    run = ShadowConsumer(bus, resolver, FakeClient(envelope(), envelope()), sink).run_once()
    assert (run.seen, run.evaluated, run.skipped) == (2, 2, 0)
    assert len(sink.records) == 2
    assert sink.cursor() == 2


def test_consumer_only_asks_for_routing_events():
    bus = FakeBus([routed(1)])
    ShadowConsumer(bus, FakeResolver({}), FakeClient(), MemoryShadowSink()).run_once()
    assert bus.queries[0]["types"] == [ROUTED_EVENT_TYPE]


def test_consumer_resumes_from_the_cursor_exclusively():
    """`seq` is monotonic but not dense: resume with the literal last value
    seen, never with arithmetic on it (EVENT_BUS.md §2)."""
    bus = FakeBus([routed(1), routed(9)])
    sink = MemoryShadowSink(cursor=1)
    resolver = FakeResolver({9: shadow_input()})
    run = ShadowConsumer(bus, resolver, FakeClient(envelope()), sink).run_once()
    assert bus.queries[0]["since"] == 1
    assert run.seen == 1 and sink.cursor() == 9


def test_an_unresolvable_event_is_skipped_not_wedged():
    """Holding the cursor would stall the consumer forever on one bad row."""
    bus = FakeBus([routed(1), routed(2)])
    resolver = FakeResolver({2: shadow_input()})  # seq 1 resolves to None
    sink = MemoryShadowSink()
    run = ShadowConsumer(bus, resolver, FakeClient(envelope()), sink).run_once()
    assert run.skipped == 1 and run.evaluated == 1
    assert sink.cursor() == 2


def test_a_raising_resolver_is_skipped_not_propagated():
    bus = FakeBus([routed(1), routed(2)])
    resolver = FakeResolver({2: shadow_input()}, raises_on={1})
    sink = MemoryShadowSink()
    run = ShadowConsumer(bus, resolver, FakeClient(envelope()), sink).run_once()
    assert run.skipped == 1
    assert sink.cursor() == 2


def test_a_bus_read_failure_leaves_the_cursor_untouched():
    sink = MemoryShadowSink(cursor=4)
    run = ShadowConsumer(FakeBus([], fail=True), FakeResolver({}),
                         FakeClient(), sink).run_once()
    assert run.errors == 1 and run.seen == 0
    assert sink.cursor() == 4, "a failed read must not skip unseen events"


def test_a_failing_sink_does_not_wedge_the_tail():
    class BrokenSink(MemoryShadowSink):
        def append(self, record):
            raise IOError("disk full")

    bus = FakeBus([routed(1)])
    sink = BrokenSink()
    run = ShadowConsumer(bus, FakeResolver({1: shadow_input()}),
                         FakeClient(envelope()), sink).run_once()
    assert run.errors == 1
    assert sink.cursor() == 1


def test_a_dead_model_never_stops_the_consumer():
    bus = FakeBus([routed(1), routed(2)])
    resolver = FakeResolver({1: shadow_input(), 2: shadow_input()})
    sink = MemoryShadowSink()
    client = FakeClient(ConnectionError("down"), ConnectionError("down"))
    run = ShadowConsumer(bus, resolver, client, sink).run_once()
    assert run.evaluated == 2 and run.errors == 2
    assert all(r["model_error"] for r in sink.records)
    assert sink.cursor() == 2


# --------------------------------------------------------------------------- #
# The report, and its denominator
# --------------------------------------------------------------------------- #
def test_agreement_rate_excludes_what_could_not_be_compared():
    """A rented route the control vocabulary cannot name says nothing about the
    model's judgement; counting it as a miss would understate a model that never
    had the chance to be right."""
    records = [
        {"agreement": "agree"}, {"agreement": "agree"}, {"agreement": "agree"},
        {"agreement": "disagree"},
        {"agreement": "unrepresentable"}, {"agreement": "unrepresentable"},
        {"agreement": "not_a_provider_route"},
        {"agreement": None, "model_error": "model call failed: boom"},
    ]
    r = summarize(records)
    assert (r.agree, r.disagree) == (3, 1)
    assert (r.unrepresentable, r.not_a_provider_route) == (2, 1)
    assert r.model_errors == 1 and r.uncompared == 1 and r.total == 8
    assert r.comparable == 4
    assert r.agreement_rate == 0.75


def test_agreement_rate_is_none_when_nothing_was_comparable():
    r = summarize([{"agreement": "unrepresentable"}, {"agreement": None}])
    assert r.agreement_rate is None, "0.0 would read as total disagreement"


def test_summarize_counts_escalations():
    r = summarize([
        {"agreement": "agree", "model": {"escalate": True}},
        {"agreement": "agree", "model": {"escalate": False}},
    ])
    assert r.escalations == 1


def test_summarize_tolerates_an_unknown_agreement_value():
    r = summarize([{"agreement": "sideways"}])
    assert r.uncompared == 1 and r.comparable == 0


def test_report_serializes():
    d = AgreementReport(agree=1, disagree=1, total=2).to_dict()
    assert d["agreement_rate"] == 0.5
    assert json.loads(json.dumps(d)) == d


def test_end_to_end_run_produces_a_readable_report():
    bus = FakeBus([routed(1), routed(2), routed(3)])
    resolver = FakeResolver({
        1: shadow_input(),
        2: shadow_input(tier="external_paid"),
        3: shadow_input(tier="private_rented"),
    })
    sink = MemoryShadowSink()
    client = FakeClient(envelope(), envelope(), envelope())
    ShadowConsumer(bus, resolver, client, sink).run_once()
    r = summarize(sink.records)
    assert (r.agree, r.disagree, r.unrepresentable) == (1, 1, 1)
    assert r.agreement_rate == 0.5


# --------------------------------------------------------------------------- #
# The Engine A bridge
# --------------------------------------------------------------------------- #
def test_worker_location_bridges_to_the_provider_vocabulary():
    assert RouterFacts.from_worker_location("local").provider_tier == "local_only"
    assert RouterFacts.from_worker_location("cloud").provider_tier == "external"
    assert RouterFacts.from_worker_location("rented").provider_tier == "private_rented"


def test_a_cloud_placement_agrees_with_either_cost_tier():
    """A WorkerLocation carries no cost signal, and every cloud_* class admits
    both tiers — so the ambiguity cancels instead of inventing a disagreement."""
    facts = RouterFacts.from_worker_location("cloud")
    for cls in ("cloud_general", "cloud_frontier", "cloud_reasoner"):
        rec = evaluate_once(
            ShadowInput(state=NormalizedState(privacy=ControlPrivacy.public), router=facts),
            FakeClient(envelope(route_class=cls)),
        )
        assert rec.agreement is Agreement.agree


def test_a_rented_placement_is_the_unrepresentable_case_end_to_end():
    rec = evaluate_once(
        ShadowInput(
            state=NormalizedState(privacy=ControlPrivacy.local_only),
            router=RouterFacts.from_worker_location("rented"),
        ),
        FakeClient(envelope()),
    )
    assert rec.agreement is Agreement.unrepresentable


@pytest.mark.parametrize("location", [None, "", "teleporter", "LOCAL"])
def test_an_unknown_placement_is_uncompared_not_guessed(location):
    facts = RouterFacts.from_worker_location(location)
    assert facts.provider_tier is None
    rec = evaluate_once(
        ShadowInput(state=NormalizedState(privacy=ControlPrivacy.public), router=facts),
        FakeClient(envelope()),
    )
    assert rec.agreement is None


def test_the_placed_event_carries_the_location_in_its_payload():
    """`compute.placed` is the better trigger of the two: the placement class is
    already in its bounded payload, so no second lookup is needed to score it."""
    event = {"seq": 5, "type": PLACED_EVENT_TYPE, "task_id": "task_1",
             "subtask_id": "subtask_1", "payload": {"location": "cloud"}}
    facts = RouterFacts.from_worker_location(event["payload"]["location"])
    assert facts.provider_tier == "external"


def test_consumer_can_tail_placement_events_instead():
    bus = FakeBus([{"seq": 1, "type": PLACED_EVENT_TYPE, "task_id": "t",
                    "subtask_id": "s", "outcome": None, "payload": {"location": "local"}}])
    resolver = FakeResolver({1: ShadowInput(
        state=NormalizedState(privacy=ControlPrivacy.local_only),
        router=RouterFacts.from_worker_location("local"),
    )})
    sink = MemoryShadowSink()
    run = ShadowConsumer(bus, resolver, FakeClient(envelope()), sink,
                         event_types=(PLACED_EVENT_TYPE,)).run_once()
    assert bus.queries[0]["types"] == [PLACED_EVENT_TYPE]
    assert run.evaluated == 1
    assert sink.records[0]["agreement"] == "agree"
