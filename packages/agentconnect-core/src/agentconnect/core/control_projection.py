"""Control-model vocabulary projection (ADR 0010).

The control model (`connect-control-model`, formerly `brainconnect-control-model`)
emits decisions in a vocabulary of its own: four privacy values and eleven
capability classes. AgentConnect speaks three *other* privacy vocabularies:

* ``PrivacyClass``        — task-payload classification, used by the provider
  router (``agentconnect.router.routing``). Five values.
* ``PrivacyTier``         — subtask/worker placement, used by the core worker
  router and mirrored verbatim by ComputeConnect. Five values.
* ``ProviderPrivacyTier`` — what a *provider* is, not what a payload needs. Four
  values.

Shadow mode has to compare a control-model ROUTE decision against the
deterministic router's ``RoutingDecision``. It cannot do that until those
vocabularies are projected onto one another, and an ad-hoc projection written at
the comparison site is exactly where a privacy widening would hide. This module
is that projection, in one place, with the widening rule enforced by tests.

**The rule: a projection never widens permission.** Where a source value has no
exact counterpart, it maps to the *strictest* compatible target — never the
loosest, never the "closest looking". A projection that cannot be made
faithfully says so (``Projection.faithful is False``) rather than guessing; this
is the same posture as ``computeconnect.placement``'s structured refusal, for the
same reason: a silent downgrade is worse than a visible gap.

**This module holds no authority.** It converts vocabularies. Every hard
constraint is still enforced by the deterministic routers against their own
vocabulary, on values they resolved themselves. Nothing here is consulted at
enforcement time, and no control-model output reaches a Governance Decision
Record through it (ADR 0010 §3).

Pure functions over enums: no I/O, no clock, no randomness, no config.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..common.schemas import PrivacyClass, ProviderPrivacyTier
from .models import PRIVACY_STRICTNESS, PrivacyTier

#: Upstream source of truth for the mirrored vocabularies below. The control
#: model is a separate repository and deliberately *not* a dependency of
#: AgentConnect (it is a GGUF artifact served over HTTP, not an importable
#: package), so its enums are mirrored here. ``test_control_projection.py`` pins
#: the exact literal strings; if the upstream ontology changes, that test fails
#: rather than the projection silently mapping a value that no longer exists.
CONTROL_ONTOLOGY_SOURCE = "connect-control-model:data/schemas/ontology.py"

#: Schema version of the control-model decision envelope this projection targets.
CONTROL_SCHEMA_VERSION = 1


class ControlPrivacy(str, Enum):
    """Mirror of the control model's ``PRIVACY`` enum."""

    public = "public"
    local_preferred = "local_preferred"
    local_only = "local_only"
    secret_involved = "secret_involved"


class ControlCapabilityClass(str, Enum):
    """Mirror of the control model's ``CAPABILITY_CLASS`` enum.

    These are worker *capability classes*, not provider names — the control model
    never names a provider.
    """

    deterministic_rule = "deterministic_rule"
    embedded_controller = "embedded_controller"
    inference_box_general = "inference_box_general"
    inference_box_repository_coder = "inference_box_repository_coder"
    inference_box_reasoner = "inference_box_reasoner"
    inference_box_vision = "inference_box_vision"
    cloud_general = "cloud_general"
    cloud_repository_coder = "cloud_repository_coder"
    cloud_reasoner = "cloud_reasoner"
    cloud_vision = "cloud_vision"
    cloud_frontier = "cloud_frontier"


#: Strictness order for the control vocabulary, loosest first. Mirrors the
#: oracle's stated policy priority ("security / privacy > capability > cost >
#: latency > locality").
CONTROL_STRICTNESS: dict[ControlPrivacy, int] = {
    ControlPrivacy.public: 0,
    ControlPrivacy.local_preferred: 1,
    ControlPrivacy.local_only: 2,
    ControlPrivacy.secret_involved: 3,
}

#: Strictness order for ``PrivacyClass``, loosest first.
#:
#: **Projection-local, and derived rather than declared.** AgentConnect does not
#: define a strictness order for ``PrivacyClass`` upstream; this one is read off
#: the hard constraints in ``router/routing.py``: ``low_sensitive`` needs a
#: passing redaction pass before cloud, ``restricted`` is local-capable but never
#: external, and ``secret_sensitive`` can block LLM routing entirely
#: (``privacy_class_blocks_all_llm_routing``). It exists so the non-widening rule
#: can be *checked*, and is not a new policy input: no router consults it.
PRIVACY_CLASS_STRICTNESS: dict[PrivacyClass, int] = {
    PrivacyClass.public: 0,
    PrivacyClass.low_sensitive: 1,
    PrivacyClass.repo_sensitive: 2,
    PrivacyClass.restricted: 3,
    PrivacyClass.secret_sensitive: 4,
}


@dataclass(frozen=True)
class Projection:
    """The result of projecting one vocabulary's value onto another.

    ``values`` is the set of target values compatible with ``source`` — a set,
    not a single value, because the target vocabulary often draws a distinction
    the source cannot express (every ``cloud_*`` class is compatible with both
    ``external`` and ``external_paid``; the control model cannot see cost).

    ``faithful`` separates the two ways ``values`` can be empty:

    * ``faithful=True`` with no values — the source genuinely maps to nothing in
      the target vocabulary. ``deterministic_rule`` is not a provider; that is an
      answer, not a gap.
    * ``faithful=False`` — information was lost. The source names something the
      target vocabulary cannot express at all. Callers must not treat this as a
      mismatch or score it as a disagreement; it is a hole in the vocabulary.
    """

    source: str
    values: tuple[str, ...]
    faithful: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "values": list(self.values),
            "faithful": self.faithful,
            "detail": self.detail,
        }


class Agreement(str, Enum):
    """Whether a control-model route matches the deterministic router's."""

    agree = "agree"
    disagree = "disagree"
    #: The control model routed to something that is not a provider at all
    #: (a deterministic rule, or itself). Not comparable to a provider choice.
    not_a_provider_route = "not_a_provider_route"
    #: One side named something the other vocabulary cannot express. Excluded
    #: from agreement rates rather than counted against either side.
    unrepresentable = "unrepresentable"


# --------------------------------------------------------------------------- #
# Privacy: control  <->  PrivacyClass
# --------------------------------------------------------------------------- #
#: ``local_preferred`` is a *preference*; ``low_sensitive`` is a *constraint*
#: (cloud only after a passing redaction pass). Mapping a preference onto a
#: constraint narrows, which is the safe direction. ``local_only`` maps to
#: ``restricted`` — the strictest class that still permits local LLM routing —
#: rather than ``repo_sensitive``, which the router lets reach rented hardware.
_CONTROL_TO_CLASS: dict[ControlPrivacy, PrivacyClass] = {
    ControlPrivacy.public: PrivacyClass.public,
    ControlPrivacy.local_preferred: PrivacyClass.low_sensitive,
    ControlPrivacy.local_only: PrivacyClass.restricted,
    ControlPrivacy.secret_involved: PrivacyClass.secret_sensitive,
}

#: Reverse. ``repo_sensitive`` has no control counterpart, so it collapses onto
#: ``local_only`` (stricter) rather than ``local_preferred`` (looser).
_CLASS_TO_CONTROL: dict[PrivacyClass, ControlPrivacy] = {
    PrivacyClass.public: ControlPrivacy.public,
    PrivacyClass.low_sensitive: ControlPrivacy.local_preferred,
    PrivacyClass.repo_sensitive: ControlPrivacy.local_only,
    PrivacyClass.restricted: ControlPrivacy.local_only,
    PrivacyClass.secret_sensitive: ControlPrivacy.secret_involved,
}


# --------------------------------------------------------------------------- #
# Privacy: control  <->  PrivacyTier
# --------------------------------------------------------------------------- #
#: ``local_preferred`` maps to ``repo_sensitive``, **not** ``public_redacted``.
#: ``public_redacted`` is one of ComputeConnect's two cloud-permitting tiers
#: (``computeconnect.privacy.CLOUD_PERMITTING_TIERS``), so projecting a
#: local-preferring value onto it would hand cloud eligibility to a task that
#: never asked for it — the exact widening this module exists to prevent.
_CONTROL_TO_TIER: dict[ControlPrivacy, PrivacyTier] = {
    ControlPrivacy.public: PrivacyTier.public,
    ControlPrivacy.local_preferred: PrivacyTier.repo_sensitive,
    ControlPrivacy.local_only: PrivacyTier.local_only,
    ControlPrivacy.secret_involved: PrivacyTier.secret_sensitive,
}

_TIER_TO_CONTROL: dict[PrivacyTier, ControlPrivacy] = {
    PrivacyTier.public: ControlPrivacy.public,
    PrivacyTier.public_redacted: ControlPrivacy.local_preferred,
    PrivacyTier.repo_sensitive: ControlPrivacy.local_only,
    PrivacyTier.local_only: ControlPrivacy.local_only,
    PrivacyTier.secret_sensitive: ControlPrivacy.secret_involved,
}


def control_privacy_to_class(value: ControlPrivacy) -> PrivacyClass:
    """Project a control-model privacy value onto ``PrivacyClass``."""
    return _CONTROL_TO_CLASS[ControlPrivacy(value)]


def control_privacy_to_tier(value: ControlPrivacy) -> PrivacyTier:
    """Project a control-model privacy value onto ``PrivacyTier``."""
    return _CONTROL_TO_TIER[ControlPrivacy(value)]


def class_to_control_privacy(value: PrivacyClass) -> ControlPrivacy:
    """Project a ``PrivacyClass`` onto the control-model vocabulary.

    Used when building the normalized state handed *to* the model.
    """
    return _CLASS_TO_CONTROL[PrivacyClass(value)]


def tier_to_control_privacy(value: PrivacyTier) -> ControlPrivacy:
    """Project a ``PrivacyTier`` onto the control-model vocabulary."""
    return _TIER_TO_CONTROL[PrivacyTier(value)]


# --------------------------------------------------------------------------- #
# Capability classes  <->  provider tiers
# --------------------------------------------------------------------------- #
#: Classes that name no provider at all. ``embedded_controller`` is the control
#: model itself, which the model card forbids from doing substantive work.
NON_PROVIDER_CLASSES = frozenset(
    {
        ControlCapabilityClass.deterministic_rule,
        ControlCapabilityClass.embedded_controller,
    }
)

#: The capability families the class names encode, in suffix form.
CAPABILITY_FAMILIES = ("general", "repository_coder", "reasoner", "vision", "frontier")

_INFERENCE_BOX_CLASSES = frozenset(
    c for c in ControlCapabilityClass if c.value.startswith("inference_box_")
)
_CLOUD_CLASSES = frozenset(
    c for c in ControlCapabilityClass if c.value.startswith("cloud_")
)


def is_provider_route(value: ControlCapabilityClass) -> bool:
    """True when the class names a provider the router could also have picked."""
    return ControlCapabilityClass(value) not in NON_PROVIDER_CLASSES


def capability_family(value: ControlCapabilityClass) -> str | None:
    """The capability family a class encodes, or None for non-provider classes.

    Returned as a bare string rather than an enum on purpose: AgentConnect's
    capability names are operator configuration (``profiles.yaml``), not a fixed
    vocabulary, so mapping a family onto a deployment's capability strings is the
    caller's job. Inventing a fixed table here would be inventing policy.
    """
    cls = ControlCapabilityClass(value)
    if cls in NON_PROVIDER_CLASSES:
        return None
    prefix = "inference_box_" if cls in _INFERENCE_BOX_CLASSES else "cloud_"
    return cls.value[len(prefix) :]


def capability_class_to_provider_tiers(value: ControlCapabilityClass) -> Projection:
    """Project a control capability class onto the provider tiers it admits.

    ``cloud_*`` admits both ``external`` and ``external_paid``: the control model
    is given no cost or quota signal, so it cannot distinguish a free tier from a
    paid one, and collapsing to either single value would manufacture a
    disagreement (or a spend) out of a distinction the model never saw.
    """
    cls = ControlCapabilityClass(value)
    if cls in NON_PROVIDER_CLASSES:
        return Projection(
            source=cls.value,
            values=(),
            faithful=True,
            detail="not a provider route: no model call is dispatched for this class",
        )
    if cls in _INFERENCE_BOX_CLASSES:
        return Projection(
            source=cls.value,
            values=(ProviderPrivacyTier.local_only.value,),
            faithful=True,
        )
    return Projection(
        source=cls.value,
        values=(
            ProviderPrivacyTier.external.value,
            ProviderPrivacyTier.external_paid.value,
        ),
        faithful=True,
        detail="cost tier undetermined: the control model receives no cost or quota signal",
    )


def provider_tier_to_capability_classes(value: ProviderPrivacyTier) -> Projection:
    """Project a provider tier back onto the control capability classes.

    ``private_rented`` — your own model weights on rented hardware — has **no**
    counterpart in the control vocabulary. It is neither ``inference_box_*`` (the
    control model's owned-hardware class) nor ``cloud_*`` (someone else's model
    on someone else's hardware), and the control model was never trained to emit
    anything for it. This returns ``faithful=False`` rather than picking the
    nearer-looking of the two: a rented route scored as an ``inference_box``
    match would report agreement the model never expressed, and scored as a
    mismatch would charge the model for a class it cannot name.
    """
    tier = ProviderPrivacyTier(value)
    if tier is ProviderPrivacyTier.local_only:
        return Projection(
            source=tier.value,
            values=tuple(sorted(c.value for c in _INFERENCE_BOX_CLASSES)),
            faithful=True,
            detail="family undetermined: pick by capability, not by tier",
        )
    if tier in (ProviderPrivacyTier.external, ProviderPrivacyTier.external_paid):
        return Projection(
            source=tier.value,
            values=tuple(sorted(c.value for c in _CLOUD_CLASSES)),
            faithful=True,
            detail="family undetermined: pick by capability, not by tier",
        )
    return Projection(
        source=tier.value,
        values=(),
        faithful=False,
        detail=(
            "the control-model vocabulary has no class for your own weights on "
            "rented hardware; it distinguishes only owned inference boxes from "
            "third-party cloud models (schema v1)"
        ),
    )


def route_agreement(
    control_route_class: ControlCapabilityClass,
    router_provider_tier: ProviderPrivacyTier,
) -> Agreement:
    """Compare a control-model ROUTE decision with the router's chosen provider.

    The caller resolves ``RoutingDecision.selected_provider`` to its configured
    ``ProviderPrivacyTier`` and passes that in; this stays a pure function over
    two enums so shadow-mode scoring has no registry dependency.

    A ``private_rented`` selection yields ``unrepresentable``, not ``disagree`` —
    otherwise every rented-GPU route would be counted as a model error and the
    shadow-mode agreement rate would understate the model for a gap in the
    vocabulary rather than a fault in its decision.
    """
    cls = ControlCapabilityClass(control_route_class)
    tier = ProviderPrivacyTier(router_provider_tier)

    if not is_provider_route(cls):
        return Agreement.not_a_provider_route

    reverse = provider_tier_to_capability_classes(tier)
    if not reverse.faithful:
        return Agreement.unrepresentable

    admitted = capability_class_to_provider_tiers(cls)
    return Agreement.agree if tier.value in admitted.values else Agreement.disagree


# --------------------------------------------------------------------------- #
# The widening rule, as a callable check
# --------------------------------------------------------------------------- #
def widens_control(before: ControlPrivacy, after: ControlPrivacy) -> bool:
    """True if ``after`` is *looser* than ``before`` on the control scale."""
    return CONTROL_STRICTNESS[ControlPrivacy(after)] < CONTROL_STRICTNESS[
        ControlPrivacy(before)
    ]


def widens_class(before: PrivacyClass, after: PrivacyClass) -> bool:
    """True if ``after`` is *looser* than ``before`` on the PrivacyClass scale."""
    return PRIVACY_CLASS_STRICTNESS[PrivacyClass(after)] < PRIVACY_CLASS_STRICTNESS[
        PrivacyClass(before)
    ]


def widens_tier(before: PrivacyTier, after: PrivacyTier) -> bool:
    """True if ``after`` is *looser* than ``before`` on the PrivacyTier scale."""
    return PRIVACY_STRICTNESS[PrivacyTier(after)] < PRIVACY_STRICTNESS[
        PrivacyTier(before)
    ]
