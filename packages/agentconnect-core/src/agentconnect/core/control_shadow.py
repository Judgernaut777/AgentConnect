"""Control-model shadow mode (ADR 0010 §3, §4).

Shadow mode is how the control model earns — or fails to earn — a promotion. It
watches routing decisions the deterministic router has *already made*, asks the
model what it would have done, and records the pair for offline comparison. The
model decides nothing. Nothing waits on it.

The shape of one record is the quadruple the model's own handoff asks for::

    (normalized_state, model_decision, router_decision, outcome)

Four properties hold structurally here, not by convention:

**Nothing in the routing path imports this module.** The consumer is driven
separately — from the CLI, a timer, whatever — and reads the event bus after
the fact. `POST /route/decide` answers in microseconds and must keep doing so;
a ~5 s model call has no place in front of it (ADR 0010 §4).

**The bus is a wake signal, never the data.** Bus payloads are deliberately
bounded and privacy-safe — `subtask.routed` carries a worker id and nothing
else — so an event tells this module *that* a routing happened and which ids it
touched. The facts come back from the owning ledger through a
:class:`RoutingFactsResolver`, which is why the bus doctrine ("never consulted
to make a decision", EVENT_BUS.md §0) is not strained: nothing here decides.

**A model failure is a recorded outcome, never an exception.**
:func:`evaluate_once` returns a :class:`ShadowOutcome` carrying `model_error`
for every failure mode — unreachable server, malformed JSON, a decision that
fails the schema after its one repair attempt. Shadow mode observing nothing is
a data gap; shadow mode raising into its caller would be a shadow feature
breaking a real one.

**Only projected vocabulary crosses the boundary.** Every privacy value is
converted by :mod:`agentconnect.core.control_projection`, whose widening rule is
enforced by property tests. :data:`SHADOW_STATE_KEYS` bounds what may appear in
a normalized state, so no prompt, transcript, or free-form field can ride along
to the model — the same rule the bus applies to its own payloads.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable

from .control_projection import (
    CONTROL_SCHEMA_VERSION,
    Agreement,
    ControlCapabilityClass,
    ControlPrivacy,
    class_to_control_privacy,
    route_agreement,
)

log = logging.getLogger(__name__)

#: The bus event that means "the deterministic router just chose a worker".
ROUTED_EVENT_TYPE = "subtask.routed"

#: Emitted immediately after `subtask.routed` for the same subtask, and the
#: more useful trigger of the two: its bounded payload names the placement
#: class outright (`{"location": "local" | "cloud" | "rented"}`), so a resolver
#: can score an event without a second lookup.
PLACED_EVENT_TYPE = "compute.placed"

#: Engine A's worker vocabulary, bridged to the provider tiers
#: :func:`~agentconnect.core.control_projection.route_agreement` speaks.
#:
#: This bridge exists because the two routers in this repository are not the
#: same router. `subtask.routed` / `compute.placed` come from Engine A's
#: *worker* router (`core/routing.py`), whose vocabulary is `WorkerLocation`.
#: The *provider* router (`router/routing.py`), whose `RoutingDecision` carries
#: a `ProviderPrivacyTier`, emits nothing onto the bus at all — the Engine B
#: bridge carries only `state.changed` ticket rows. So the decision shadow mode
#: would most like to compare against is not observable today, and this maps
#: what *is*.
#:
#: `cloud` resolves to `external` rather than `external_paid` because a
#: `WorkerLocation` carries no cost signal. That is safe for scoring rather
#: than merely convenient: every `cloud_*` capability class admits **both**
#: cost tiers, so the comparison agrees on either — the ambiguity cancels
#: instead of manufacturing a disagreement. It does mean a recorded
#: `provider_tier` of `external` says "some cloud", not "a free tier".
WORKER_LOCATION_TO_PROVIDER_TIER = {
    "local": "local_only",
    "cloud": "external",
    "rented": "private_rented",
}

#: Hard output bound for a control-model call. The model card's limitation
#: "bounded output under OOD/adversarial: may fail to stop" makes this
#: load-bearing, not a tuning knob.
MAX_DECISION_TOKENS = 192

#: Sampling is off. A shadow record is only worth comparing if the same state
#: gives the same decision.
TEMPERATURE = 0.0

#: The envelope caps from the control-model schema (SCHEMA.md: "≤ 6").
MAX_REASON_CODES = 6
MAX_DETECTED_CONSTRAINTS = 6

#: Every key a normalized state may carry. A state is rejected if it holds
#: anything else — the guard that keeps prompts and transcripts out of a
#: model call, checked rather than trusted.
SHADOW_STATE_KEYS = frozenset(
    {
        "privacy",
        "needed_capabilities",
        "est_input_tokens",
        "est_output_tokens",
        "allow_external",
        "allow_paid",
        "allow_rented",
        "priority",
        "quality",
        "cloud_safe",
        "pending_same_model_batch",
    }
)


class DecisionInvalid(Exception):
    """The model returned something that is not a valid control decision."""


@dataclass(frozen=True)
class NormalizedState:
    """The compact, projected state handed to the model.

    Deliberately *not* a `RoutingContext`: that carries a task id and other
    fields the model has no business seeing, and its privacy value is in
    AgentConnect's vocabulary rather than the model's. Build one with
    :meth:`from_routing_context`.
    """

    privacy: ControlPrivacy
    needed_capabilities: tuple[str, ...] = ()
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    allow_external: bool = True
    allow_paid: bool = False
    allow_rented: bool = False
    priority: str = "normal"
    quality: str = "standard"
    cloud_safe: bool = True
    pending_same_model_batch: int = 0

    @classmethod
    def from_routing_context(cls, ctx: Any) -> "NormalizedState":
        """Project a router `RoutingContext` into the model's vocabulary.

        Duck-typed on purpose: `RoutingContext` lives in the optional
        `agentconnect-router` package, and `agentconnect-core` must import
        without it. Only the fields listed in :data:`SHADOW_STATE_KEYS` are
        read — `task_id`, `profile`, and `require_exact_model` are left behind
        rather than forwarded to a model that has no use for them.
        """
        return cls(
            privacy=class_to_control_privacy(getattr(ctx, "privacy_class")),
            needed_capabilities=tuple(getattr(ctx, "needed_capabilities", ()) or ()),
            est_input_tokens=int(getattr(ctx, "est_input_tokens", 0) or 0),
            est_output_tokens=int(getattr(ctx, "est_output_tokens", 0) or 0),
            allow_external=bool(getattr(ctx, "allow_external", True)),
            allow_paid=bool(getattr(ctx, "allow_paid", False)),
            allow_rented=bool(getattr(ctx, "allow_rented", False)),
            priority=str(getattr(getattr(ctx, "priority", "normal"), "value", None)
                         or getattr(ctx, "priority", "normal")),
            quality=str(getattr(ctx, "quality", "standard")),
            cloud_safe=bool(getattr(ctx, "cloud_safe", True)),
            pending_same_model_batch=int(getattr(ctx, "pending_same_model_batch", 0) or 0),
        )

    def to_payload(self) -> dict[str, Any]:
        """The JSON object the model sees. Keys are bounded by
        :data:`SHADOW_STATE_KEYS`; values are scalars and short strings."""
        payload = {
            "privacy": ControlPrivacy(self.privacy).value,
            "needed_capabilities": list(self.needed_capabilities),
            "est_input_tokens": int(self.est_input_tokens),
            "est_output_tokens": int(self.est_output_tokens),
            "allow_external": bool(self.allow_external),
            "allow_paid": bool(self.allow_paid),
            "allow_rented": bool(self.allow_rented),
            "priority": str(self.priority),
            "quality": str(self.quality),
            "cloud_safe": bool(self.cloud_safe),
            "pending_same_model_batch": int(self.pending_same_model_batch),
        }
        extra = set(payload) - SHADOW_STATE_KEYS
        if extra:  # pragma: no cover - structural guard
            raise ValueError(f"normalized state carries unlisted keys: {sorted(extra)}")
        return payload


@dataclass(frozen=True)
class ShadowDecision:
    """A parsed, schema-valid control-model decision."""

    mode: str
    route_class: Optional[ControlCapabilityClass]
    confidence: float
    reason_codes: tuple[str, ...]
    detected_constraints: tuple[str, ...]
    escalate: bool

    @property
    def should_escalate(self) -> bool:
        """Gate on the flag, never on `confidence`.

        The model's calibration report is explicit that confidence has low
        resolution (concentrated 0.8–1.0), so a threshold on it would be a
        coin flip wearing a number. The flag and the reason codes are the
        signal.
        """
        return bool(self.escalate)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "route_class": self.route_class.value if self.route_class else None,
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
            "detected_constraints": list(self.detected_constraints),
            "escalate": self.escalate,
        }


def parse_decision(raw: object, expect_mode: str = "ROUTE") -> ShadowDecision:
    """Validate one control-model decision envelope, strictly.

    Every rejection is a :class:`DecisionInvalid`; nothing is coerced and no
    field defaults into existence. A decision that does not parse is not a
    quieter decision — it is no decision, and shadow mode records it as one.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise DecisionInvalid(f"not JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DecisionInvalid(f"envelope must be an object, got {type(raw).__name__}")

    version = raw.get("schema_version")
    if version != CONTROL_SCHEMA_VERSION:
        raise DecisionInvalid(
            f"schema_version {version!r} != {CONTROL_SCHEMA_VERSION} "
            "(this projection targets schema v1 only)"
        )

    mode = raw.get("mode")
    if mode != expect_mode:
        raise DecisionInvalid(f"mode {mode!r} != requested {expect_mode!r}")

    decision = raw.get("decision")
    if not isinstance(decision, Mapping):
        raise DecisionInvalid("decision must be an object")

    route_class: Optional[ControlCapabilityClass] = None
    if expect_mode == "ROUTE":
        try:
            route_class = ControlCapabilityClass(decision.get("route_class"))
        except ValueError as exc:
            raise DecisionInvalid(
                f"unknown route_class {decision.get('route_class')!r}"
            ) from exc

    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise DecisionInvalid("confidence must be a number")
    if not 0.0 <= float(confidence) <= 1.0:
        raise DecisionInvalid(f"confidence {confidence!r} outside [0,1]")

    codes = _bounded_str_list(raw.get("reason_codes"), "reason_codes", MAX_REASON_CODES)
    constraints = _bounded_str_list(
        raw.get("detected_constraints"), "detected_constraints", MAX_DETECTED_CONSTRAINTS
    )

    escalate = raw.get("escalate")
    if not isinstance(escalate, bool):
        raise DecisionInvalid("escalate must be a boolean")

    return ShadowDecision(
        mode=mode,
        route_class=route_class,
        confidence=float(confidence),
        reason_codes=codes,
        detected_constraints=constraints,
        escalate=escalate,
    )


def _bounded_str_list(value: object, name: str, cap: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DecisionInvalid(f"{name} must be a list")
    if len(value) > cap:
        raise DecisionInvalid(f"{name} carries {len(value)} entries, cap is {cap}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DecisionInvalid(f"{name} entries must be strings")
        out.append(item)
    return tuple(out)


@runtime_checkable
class ControlModelClient(Protocol):
    """The only thing in shadow mode that talks to a model.

    A Protocol so every test drives a fake and the suite stays offline — the
    same posture as the memory, compute, and tool clients elsewhere in this
    package.
    """

    def decide(self, mode: str, state: Mapping[str, Any], repair: str = "") -> object:
        """Return the model's raw reply (a JSON string or a decoded object).

        ``repair`` carries the rejection text on the single retry
        :func:`evaluate_once` allows, so a client can append it as a follow-up
        turn. Raise any exception to signal an unreachable or failed call.
        """


@dataclass(frozen=True)
class RouterFacts:
    """What the deterministic router actually did, as shadow mode compares it.

    ``provider_tier`` is the caller's registry lookup of ``selected_provider``.
    Keeping it a plain value means this module never holds a registry, and the
    comparison stays a pure function of two enums.
    """

    decision: str
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    provider_tier: Optional[str] = None
    policy_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "provider_tier": self.provider_tier,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_worker_location(
        cls,
        location: Optional[str],
        *,
        decision: str = "routed",
        worker: Optional[str] = None,
        model: Optional[str] = None,
        policy_version: str = "unknown",
    ) -> "RouterFacts":
        """Build facts from an Engine A placement class.

        An unknown or missing location yields ``provider_tier=None``, which
        :func:`evaluate_once` records as *uncompared* rather than guessing a
        tier — the same refusal-over-guess rule the projection module follows.
        """
        tier = WORKER_LOCATION_TO_PROVIDER_TIER.get(str(location)) if location else None
        return cls(
            decision=decision,
            selected_provider=worker,
            selected_model=model,
            provider_tier=tier,
            policy_version=policy_version,
        )


@dataclass(frozen=True)
class ShadowInput:
    """One routing event, resolved into everything a comparison needs."""

    state: NormalizedState
    router: RouterFacts
    task_id: Optional[str] = None
    subtask_id: Optional[str] = None


@runtime_checkable
class RoutingFactsResolver(Protocol):
    """Turns a bus event into a :class:`ShadowInput` by reading the ledger.

    The bus event is a pointer; the ledger is the authority. Implementations
    live where the ledger and provider registry do, which keeps this module
    free of both. Return ``None`` to skip an event that cannot be resolved.
    """

    def resolve(self, event: Mapping[str, Any]) -> Optional[ShadowInput]: ...


@dataclass(frozen=True)
class ShadowOutcome:
    """One shadow record: the quadruple, plus how the two decisions compared."""

    seq: int
    state: dict[str, Any]
    router: dict[str, Any]
    model: Optional[dict[str, Any]] = None
    model_error: Optional[str] = None
    agreement: Optional[Agreement] = None
    outcome: Optional[str] = None
    task_id: Optional[str] = None
    subtask_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "state": self.state,
            "model": self.model,
            "model_error": self.model_error,
            "router": self.router,
            "agreement": self.agreement.value if self.agreement else None,
            "outcome": self.outcome,
        }


def evaluate_once(
    shadow_input: ShadowInput,
    client: ControlModelClient,
    *,
    seq: int = 0,
    outcome: Optional[str] = None,
) -> ShadowOutcome:
    """Ask the model, compare, and return the record. Never raises.

    One bounded repair attempt on a schema violation, exactly as the model's
    handoff prescribes ("on invalid JSON, do one bounded repair retry, else
    deterministic fallback"). The deterministic fallback here *is* the
    recorded gap: shadow mode has no decision to fall back to, because it was
    never making one.
    """
    state_payload = shadow_input.state.to_payload()
    router_payload = shadow_input.router.to_dict()
    base = {
        "seq": seq,
        "state": state_payload,
        "router": router_payload,
        "outcome": outcome,
        "task_id": shadow_input.task_id,
        "subtask_id": shadow_input.subtask_id,
    }

    decision: Optional[ShadowDecision] = None
    error: Optional[str] = None
    repair = ""
    for attempt in (1, 2):
        try:
            raw = client.decide("ROUTE", state_payload, repair=repair)
            decision = parse_decision(raw, expect_mode="ROUTE")
            error = None
            break
        except DecisionInvalid as exc:
            error = f"invalid decision: {exc}"
            repair = str(exc)
            if attempt == 2:
                break
        except Exception as exc:  # the model is unreachable/failing: record it
            error = f"model call failed: {type(exc).__name__}: {exc}"
            break

    if decision is None:
        log.debug("shadow: no usable decision at seq %s (%s)", seq, error)
        return ShadowOutcome(model=None, model_error=error, agreement=None, **base)

    agreement = _score(decision, shadow_input.router)
    return ShadowOutcome(
        model=decision.to_dict(), model_error=None, agreement=agreement, **base
    )


def _score(decision: ShadowDecision, router: RouterFacts) -> Optional[Agreement]:
    """Compare, or decline to. ``None`` means there was nothing to compare."""
    if decision.route_class is None or router.provider_tier is None:
        return None
    try:
        return route_agreement(decision.route_class, router.provider_tier)
    except ValueError:
        # A provider tier this build does not know is a gap in the comparison,
        # not a verdict against either side.
        return Agreement.unrepresentable


@runtime_checkable
class ShadowSink(Protocol):
    """Append-only store for shadow records, plus the consumer's bus cursor.

    Deliberately not the governed ledger. ADR 0010 §3 keeps control-model
    output out of any Decision Record, and a sink the Kernel never reads is
    the structural way to honor that rather than a rule someone must remember.
    """

    def append(self, record: Mapping[str, Any]) -> None: ...

    def cursor(self) -> int: ...

    def set_cursor(self, seq: int) -> None: ...


class MemoryShadowSink:
    """An in-process sink. The default for tests and for a dry run."""

    def __init__(self, cursor: int = 0) -> None:
        self.records: list[dict[str, Any]] = []
        self._cursor = int(cursor)

    def append(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))

    def cursor(self) -> int:
        return self._cursor

    def set_cursor(self, seq: int) -> None:
        self._cursor = int(seq)


@dataclass
class ConsumerRun:
    """What one pass over the bus did."""

    seen: int = 0
    evaluated: int = 0
    skipped: int = 0
    errors: int = 0
    cursor: int = 0


class ShadowConsumer:
    """Tails routing events and records a shadow comparison for each.

    Reads through ``list_bus_events(since=..., types=[...])`` — any object with
    that method, so the ledger's storage handle drops straight in and a fake
    serves the tests.
    """

    def __init__(
        self,
        events: Any,
        resolver: RoutingFactsResolver,
        client: ControlModelClient,
        sink: ShadowSink,
        *,
        event_types: Iterable[str] = (ROUTED_EVENT_TYPE,),
    ) -> None:
        self._events = events
        self._resolver = resolver
        self._client = client
        self._sink = sink
        self._types = list(event_types)

    def run_once(self, limit: int = 50) -> ConsumerRun:
        """Process one batch. Never raises; always advances the cursor.

        Advancing past an event this pass could not resolve is deliberate. The
        alternative — hold the cursor and retry — wedges the consumer forever
        on one bad row, and a wedged shadow evaluator is worse than a gap in
        an evaluation dataset. Skips are counted so the gap is visible.
        """
        run = ConsumerRun(cursor=self._sink.cursor())
        try:
            batch = self._events.list_bus_events(
                since=run.cursor, limit=limit, types=self._types
            )
        except Exception as exc:  # a bus read failure leaves the cursor put
            log.warning("shadow: bus read failed at seq %s: %s", run.cursor, exc)
            run.errors += 1
            return run

        for event in batch:
            run.seen += 1
            seq = int(event.get("seq", run.cursor))
            try:
                resolved = self._resolver.resolve(event)
            except Exception as exc:
                log.debug("shadow: resolver failed at seq %s: %s", seq, exc)
                resolved = None
            if resolved is None:
                run.skipped += 1
                run.cursor = seq
                self._sink.set_cursor(seq)
                continue

            record = evaluate_once(
                resolved, self._client, seq=seq, outcome=event.get("outcome")
            )
            if record.model_error:
                run.errors += 1
            try:
                self._sink.append(record.to_dict())
            except Exception as exc:  # a sink failure must not wedge the tail
                log.warning("shadow: sink append failed at seq %s: %s", seq, exc)
                run.errors += 1
            run.evaluated += 1
            run.cursor = seq
            self._sink.set_cursor(seq)

        return run


@dataclass
class AgreementReport:
    """The shadow metric, with the denominator stated rather than assumed."""

    agree: int = 0
    disagree: int = 0
    unrepresentable: int = 0
    not_a_provider_route: int = 0
    uncompared: int = 0
    model_errors: int = 0
    escalations: int = 0
    total: int = 0

    @property
    def comparable(self) -> int:
        """Records where both sides named something the other can express."""
        return self.agree + self.disagree

    @property
    def agreement_rate(self) -> Optional[float]:
        """Agreement over *comparable* records, or None when there are none.

        `unrepresentable` and `not_a_provider_route` are excluded from the
        denominator, not counted as misses. A rented-node route the control
        vocabulary cannot name says nothing about the model's judgement, and
        folding it in would quietly understate a model that never had the
        chance to be right (ADR 0010 §5).
        """
        return (self.agree / self.comparable) if self.comparable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agree": self.agree,
            "disagree": self.disagree,
            "unrepresentable": self.unrepresentable,
            "not_a_provider_route": self.not_a_provider_route,
            "uncompared": self.uncompared,
            "model_errors": self.model_errors,
            "escalations": self.escalations,
            "total": self.total,
            "comparable": self.comparable,
            "agreement_rate": self.agreement_rate,
        }


_AGREEMENT_FIELDS = {
    Agreement.agree: "agree",
    Agreement.disagree: "disagree",
    Agreement.unrepresentable: "unrepresentable",
    Agreement.not_a_provider_route: "not_a_provider_route",
}


def summarize(records: Iterable[Mapping[str, Any]]) -> AgreementReport:
    """Aggregate shadow records into the report an operator reads."""
    report = AgreementReport()
    for rec in records:
        report.total += 1
        if rec.get("model_error"):
            report.model_errors += 1
        model = rec.get("model")
        if isinstance(model, Mapping) and model.get("escalate"):
            report.escalations += 1
        value = rec.get("agreement")
        if value is None:
            report.uncompared += 1
            continue
        try:
            field_name = _AGREEMENT_FIELDS[Agreement(value)]
        except (ValueError, KeyError):
            report.uncompared += 1
            continue
        setattr(report, field_name, getattr(report, field_name) + 1)
    return report


class HttpControlModelClient:
    """Talks to a CPU llama.cpp server over the OpenAI-compatible route.

    The prompt contract is the control model's own (`final-recommendation.md`
    step 2): a fixed system prompt, then ``MODE: <MODE>\\nSTATE:\\n<json>``.
    Sampling is off, output is hard-capped, and a stop string on the closing
    brace bounds a model that fails to stop.

    ``httpx`` is imported lazily, matching the other shipped clients in this
    package: importing this module must not require the network stack.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        system_prompt: str,
        timeout: float = 30.0,
        max_tokens: int = MAX_DECISION_TOKENS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt
        self.timeout = timeout
        self.max_tokens = int(max_tokens)

    def decide(self, mode: str, state: Mapping[str, Any], repair: str = "") -> object:
        import httpx  # lazy: only the network path needs it

        user = f"MODE: {mode}\nSTATE:\n{json.dumps(dict(state), sort_keys=True)}"
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user},
        ]
        if repair:
            messages.append(
                {"role": "user", "content": f"Rejected: {repair}\nReturn corrected JSON only."}
            )
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": self.max_tokens,
            "stop": ["}\n"],
        }
        resp = httpx.post(
            f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
