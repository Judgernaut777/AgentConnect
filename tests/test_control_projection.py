"""Control-model vocabulary projection (ADR 0010).

The load-bearing tests here are the property tests, not the table tests: a
projection between privacy vocabularies is only safe if it can never widen
permission, and "never" is a claim about every value, not the ones someone
remembered to write down.
"""

import pytest

from agentconnect.core.control_projection import (
    CONTROL_ONTOLOGY_SOURCE,
    CONTROL_SCHEMA_VERSION,
    CONTROL_STRICTNESS,
    NON_PROVIDER_CLASSES,
    PRIVACY_CLASS_STRICTNESS,
    Agreement,
    ControlCapabilityClass,
    ControlPrivacy,
    capability_class_to_provider_tiers,
    capability_family,
    class_to_control_privacy,
    control_privacy_to_class,
    control_privacy_to_tier,
    is_provider_route,
    provider_tier_to_capability_classes,
    route_agreement,
    tier_to_control_privacy,
    widens_class,
    widens_control,
    widens_tier,
)
from agentconnect.common.schemas import PrivacyClass, ProviderPrivacyTier
from agentconnect.core.models import PrivacyTier

#: Mirror of ``computeconnect.privacy.CLOUD_PERMITTING_TIERS``. ComputeConnect is
#: not an AgentConnect dependency, so the invariant is asserted against a local
#: copy; if the two ever diverge the cloud-eligibility test below is the place
#: that should start failing.
CLOUD_PERMITTING_TIERS = frozenset({"public", "public_redacted"})


# --------------------------------------------------------------------------- #
# The mirrored vocabularies match upstream
# --------------------------------------------------------------------------- #
def test_control_privacy_mirrors_upstream_ontology_exactly():
    """Pins the literal strings from ``ontology.py``. If upstream renames a
    value, this fails rather than the projection quietly mapping a dead one."""
    assert [p.value for p in ControlPrivacy] == [
        "public",
        "local_preferred",
        "local_only",
        "secret_involved",
    ]


def test_control_capability_classes_mirror_upstream_ontology_exactly():
    assert [c.value for c in ControlCapabilityClass] == [
        "deterministic_rule",
        "embedded_controller",
        "inference_box_general",
        "inference_box_repository_coder",
        "inference_box_reasoner",
        "inference_box_vision",
        "cloud_general",
        "cloud_repository_coder",
        "cloud_reasoner",
        "cloud_vision",
        "cloud_frontier",
    ]


def test_schema_version_and_source_are_recorded():
    assert CONTROL_SCHEMA_VERSION == 1
    assert CONTROL_ONTOLOGY_SOURCE.endswith("ontology.py")


def test_every_vocabulary_value_has_a_strictness_rank():
    assert set(CONTROL_STRICTNESS) == set(ControlPrivacy)
    assert set(PRIVACY_CLASS_STRICTNESS) == set(PrivacyClass)


# --------------------------------------------------------------------------- #
# Totality: no value falls through
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", list(ControlPrivacy))
def test_control_privacy_projects_onto_both_vocabularies(value):
    assert isinstance(control_privacy_to_class(value), PrivacyClass)
    assert isinstance(control_privacy_to_tier(value), PrivacyTier)


@pytest.mark.parametrize("value", list(PrivacyClass))
def test_every_privacy_class_projects_back(value):
    assert isinstance(class_to_control_privacy(value), ControlPrivacy)


@pytest.mark.parametrize("value", list(PrivacyTier))
def test_every_privacy_tier_projects_back(value):
    assert isinstance(tier_to_control_privacy(value), ControlPrivacy)


@pytest.mark.parametrize("value", list(ControlCapabilityClass))
def test_every_capability_class_projects(value):
    assert isinstance(capability_class_to_provider_tiers(value).values, tuple)


@pytest.mark.parametrize("value", list(ProviderPrivacyTier))
def test_every_provider_tier_projects(value):
    assert isinstance(provider_tier_to_capability_classes(value).values, tuple)


# --------------------------------------------------------------------------- #
# The widening rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", list(ControlPrivacy))
def test_round_trip_through_privacy_class_never_widens(value):
    back = class_to_control_privacy(control_privacy_to_class(value))
    assert not widens_control(value, back), (
        f"{value.value} -> {control_privacy_to_class(value).value} -> "
        f"{back.value} widened permission"
    )


@pytest.mark.parametrize("value", list(ControlPrivacy))
def test_round_trip_through_privacy_tier_never_widens(value):
    back = tier_to_control_privacy(control_privacy_to_tier(value))
    assert not widens_control(value, back), (
        f"{value.value} -> {control_privacy_to_tier(value).value} -> "
        f"{back.value} widened permission"
    )


@pytest.mark.parametrize("value", list(PrivacyClass))
def test_round_trip_from_privacy_class_never_widens(value):
    back = control_privacy_to_class(class_to_control_privacy(value))
    assert not widens_class(value, back)


@pytest.mark.parametrize("value", list(PrivacyTier))
def test_round_trip_from_privacy_tier_never_widens(value):
    back = control_privacy_to_tier(tier_to_control_privacy(value))
    assert not widens_tier(value, back)


@pytest.mark.parametrize("value", list(ControlPrivacy))
def test_only_public_projects_onto_a_cloud_permitting_tier(value):
    """The invariant with teeth: projecting must not hand cloud eligibility to a
    task that never asked for it. ``local_preferred`` in particular must not land
    on ``public_redacted``, which ComputeConnect treats as cloud-permitting."""
    tier = control_privacy_to_tier(value)
    if value is ControlPrivacy.public:
        assert tier.value in CLOUD_PERMITTING_TIERS
    else:
        assert tier.value not in CLOUD_PERMITTING_TIERS, (
            f"{value.value} projected onto cloud-permitting tier {tier.value}"
        )


def test_widening_helpers_detect_an_actual_widening():
    assert widens_control(ControlPrivacy.local_only, ControlPrivacy.public)
    assert not widens_control(ControlPrivacy.public, ControlPrivacy.local_only)
    assert widens_class(PrivacyClass.secret_sensitive, PrivacyClass.public)
    assert widens_tier(PrivacyTier.local_only, PrivacyTier.public)
    assert not widens_tier(PrivacyTier.public, PrivacyTier.public)


# --------------------------------------------------------------------------- #
# The specific mappings that encode a judgement
# --------------------------------------------------------------------------- #
def test_local_preferred_narrows_to_a_constraint():
    """A preference becomes a constraint, never the reverse."""
    assert control_privacy_to_class(ControlPrivacy.local_preferred) is PrivacyClass.low_sensitive
    assert control_privacy_to_tier(ControlPrivacy.local_preferred) is PrivacyTier.repo_sensitive


def test_local_only_maps_to_restricted_not_repo_sensitive():
    """``repo_sensitive`` can reach rented hardware in the router's hard
    constraints; ``local_only`` must not inherit that reach."""
    assert control_privacy_to_class(ControlPrivacy.local_only) is PrivacyClass.restricted


def test_repo_sensitive_collapses_to_the_stricter_control_value():
    """The control vocabulary has no repo tier — it must round down, not up."""
    assert class_to_control_privacy(PrivacyClass.repo_sensitive) is ControlPrivacy.local_only
    assert tier_to_control_privacy(PrivacyTier.repo_sensitive) is ControlPrivacy.local_only


# --------------------------------------------------------------------------- #
# Capability classes and provider tiers
# --------------------------------------------------------------------------- #
def test_non_provider_classes_map_to_nothing_but_faithfully():
    for cls in NON_PROVIDER_CLASSES:
        proj = capability_class_to_provider_tiers(cls)
        assert proj.values == ()
        assert proj.faithful is True, "no provider is an answer, not a gap"
        assert not is_provider_route(cls)
        assert capability_family(cls) is None


def test_inference_box_classes_admit_only_local():
    proj = capability_class_to_provider_tiers(ControlCapabilityClass.inference_box_reasoner)
    assert proj.values == (ProviderPrivacyTier.local_only.value,)
    assert proj.faithful is True


def test_cloud_classes_admit_both_cost_tiers():
    """The model gets no cost signal, so collapsing to one tier would invent a
    distinction it never saw."""
    proj = capability_class_to_provider_tiers(ControlCapabilityClass.cloud_frontier)
    assert set(proj.values) == {
        ProviderPrivacyTier.external.value,
        ProviderPrivacyTier.external_paid.value,
    }
    assert "cost" in proj.detail


@pytest.mark.parametrize(
    "cls,family",
    [
        (ControlCapabilityClass.inference_box_general, "general"),
        (ControlCapabilityClass.inference_box_repository_coder, "repository_coder"),
        (ControlCapabilityClass.cloud_vision, "vision"),
        (ControlCapabilityClass.cloud_frontier, "frontier"),
    ],
)
def test_capability_family_is_extracted_not_invented(cls, family):
    assert capability_family(cls) == family


def test_private_rented_is_unrepresentable_not_guessed():
    """The headline gap: the control model cannot name your weights on rented
    hardware, and must not be scored as if it could."""
    proj = provider_tier_to_capability_classes(ProviderPrivacyTier.private_rented)
    assert proj.faithful is False
    assert proj.values == ()
    assert "rented" in proj.detail


def test_every_other_provider_tier_is_representable():
    for tier in ProviderPrivacyTier:
        if tier is ProviderPrivacyTier.private_rented:
            continue
        assert provider_tier_to_capability_classes(tier).faithful is True


# --------------------------------------------------------------------------- #
# Shadow-mode agreement scoring
# --------------------------------------------------------------------------- #
def test_agreement_when_both_chose_local():
    assert (
        route_agreement(
            ControlCapabilityClass.inference_box_general,
            ProviderPrivacyTier.local_only,
        )
        is Agreement.agree
    )


def test_agreement_when_both_chose_cloud_regardless_of_cost_tier():
    for tier in (ProviderPrivacyTier.external, ProviderPrivacyTier.external_paid):
        assert (
            route_agreement(ControlCapabilityClass.cloud_reasoner, tier) is Agreement.agree
        )


def test_disagreement_when_model_said_local_and_router_went_cloud():
    assert (
        route_agreement(
            ControlCapabilityClass.inference_box_reasoner,
            ProviderPrivacyTier.external_paid,
        )
        is Agreement.disagree
    )


def test_rented_selection_is_excluded_not_counted_against_the_model():
    """Scoring a rented route as a miss would understate agreement for a hole in
    the vocabulary rather than a fault in the decision."""
    for cls in (
        ControlCapabilityClass.inference_box_general,
        ControlCapabilityClass.cloud_frontier,
    ):
        assert (
            route_agreement(cls, ProviderPrivacyTier.private_rented)
            is Agreement.unrepresentable
        )


def test_non_provider_route_is_not_scored_as_disagreement():
    for cls in NON_PROVIDER_CLASSES:
        assert (
            route_agreement(cls, ProviderPrivacyTier.local_only)
            is Agreement.not_a_provider_route
        )


@pytest.mark.parametrize("cls", list(ControlCapabilityClass))
@pytest.mark.parametrize("tier", list(ProviderPrivacyTier))
def test_agreement_is_total_over_the_cross_product(cls, tier):
    assert isinstance(route_agreement(cls, tier), Agreement)


# --------------------------------------------------------------------------- #
# Projection is a value, not a side effect
# --------------------------------------------------------------------------- #
def test_projection_serializes_for_the_shadow_log():
    proj = provider_tier_to_capability_classes(ProviderPrivacyTier.private_rented)
    assert proj.to_dict() == {
        "source": "private_rented",
        "values": [],
        "faithful": False,
        "detail": proj.detail,
    }


def test_projections_are_deterministic():
    """No clock, no randomness: the same input is the same output, always."""
    for cls in ControlCapabilityClass:
        assert capability_class_to_provider_tiers(cls) == capability_class_to_provider_tiers(cls)


def test_string_inputs_are_accepted_as_enum_values():
    """Shadow mode reads the model's JSON, where these arrive as bare strings."""
    assert control_privacy_to_tier("local_only") is PrivacyTier.local_only
    assert route_agreement("cloud_general", "external") is Agreement.agree


def test_unknown_vocabulary_value_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        control_privacy_to_tier("not_a_privacy_value")
    with pytest.raises(ValueError):
        route_agreement("inference_box_general", "not_a_tier")
