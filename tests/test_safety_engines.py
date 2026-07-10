"""The modular engine layer: protocol, registry, configuration, aggregation, failure.

The architecture under test:

    AgentConnect surface -> AgentConnect policy -> configured engines
      -> normalized findings -> AgentConnect enforcement

Engines detect. Policy decides. No third-party tool learns what a task is, whether
an artifact persists, or whether an audit passes.

Third-party engines are exercised here through fakes and through their `parse()`
functions on captured fixture output, so the default gate stays offline and needs no
binaries or model weights. The handful of tests that touch a real tool skip when it
is absent.
"""

import json
import shutil
import sys

import pytest

from agentconnect.safety import (
    ARTIFACT_INGEST,
    CONTEXT_OUTPUT,
    REPOSITORY_SCAN,
    Capability,
    Category,
    Decision,
    EngineConfig,
    EngineRegistry,
    EngineScanRequest,
    EngineStatus,
    Finding,
    RiskLevel,
    SafetyConfig,
    SafetyEngine,
    SafetyItem,
    SafetyPipeline,
    aggregate,
    build_engine,
)
from agentconnect.safety.engines.base import BaseEngine, ExternalToolEngine
from agentconnect.safety.engines.baseline import BaselineEngine

CLEAN = "Token expiry lives in auth/session.py."
AWS = "AKIA1234567890ABCDEF"


# --------------------------------------------------------------------- fakes
class FakeEngine(BaseEngine):
    """A configurable engine. Every failure mode the pipeline distinguishes."""

    def __init__(self, name="fake", capabilities=(Capability.secrets,), findings=(),
                 is_available=True, raises=None, version="9.9", **_):
        self.name = name
        self.version = version
        self.capabilities = frozenset(capabilities)
        self._findings = list(findings)
        self._available = is_available
        self._raises = raises
        self.scanned = 0

    def available(self):
        return self._available

    def scan(self, request):
        self.scanned += 1
        if self._raises is not None:
            raise self._raises
        return list(self._findings)


def secret_finding(engine="fake", start=0, end=len(AWS), rule="fake.aws", risk=RiskLevel.high,
                   confidence=1.0):
    return Finding(rule_id=rule, category=Category.secret, risk_level=risk,
                   message="fake", start=start, end=end, engine=engine,
                   engine_version="9.9", confidence=confidence)


def pipeline_with(*engines, required=(), surfaces=None):
    """A pipeline whose registry holds exactly the given engines."""
    names = [e.name for e in engines]
    config = SafetyConfig(
        engines={n: EngineConfig(enabled=True, required=n in required) for n in names},
        surfaces=surfaces or {})
    registry = EngineRegistry(SafetyConfig(engines={}))
    registry.config = config
    registry._engines = {e.name: e for e in engines}
    return SafetyPipeline(config, registry)


# ============================================================ 1. the protocol
def test_the_baseline_satisfies_the_engine_protocol():
    assert isinstance(BaselineEngine(), SafetyEngine)


def test_an_engine_declares_a_name_a_version_and_capabilities():
    engine = BaselineEngine()
    assert engine.name == "baseline"
    assert engine.version
    assert Capability.secrets in engine.capabilities
    assert Capability.pii not in engine.capabilities  # the baseline abstains on PII


def test_every_known_engine_declares_capabilities_without_being_installed():
    """Constructing an engine must not import its library, and must not raise when
    the library is absent."""
    from agentconnect.safety.registry import KNOWN_ENGINES

    for name in KNOWN_ENGINES:
        engine = build_engine(name)
        assert engine.name
        assert isinstance(engine.capabilities, frozenset) and engine.capabilities
        assert isinstance(engine.available(), bool)  # never raises


def test_availability_is_answered_without_scanning():
    engine = FakeEngine(is_available=False)
    assert engine.available() is False
    assert engine.scanned == 0


# ============================================================= 2. the registry
def test_an_unknown_engine_is_a_configuration_error():
    with pytest.raises(ValueError, match="unknown safety engine"):
        build_engine("detect_secrests")  # a typo, not a silently-disabled engine


def test_the_default_registry_holds_only_the_baseline():
    """A default install stays lightweight: no heavy dependency, no subprocess."""
    registry = EngineRegistry(SafetyConfig())
    assert registry.names() == ["baseline"]


def test_the_registry_selects_engines_by_capability():
    registry = EngineRegistry(SafetyConfig())
    assert [e.name for e in registry.with_capability(Capability.secrets)] == ["baseline"]
    assert registry.with_capability(Capability.pii) == []


def test_status_reports_enabled_but_unavailable_without_running_a_scan():
    """"Enabled and missing" must be visible to an operator before a scan warns."""
    config = SafetyConfig(engines={
        "baseline": EngineConfig(enabled=True, required=True),
        "gliner": EngineConfig(enabled=True),  # no model pinned -> unavailable
    })
    rows = {row["engine"]: row for row in EngineRegistry(config).status()}

    assert rows["baseline"]["available"] is True
    assert rows["gliner"]["enabled"] is True
    assert rows["gliner"]["available"] is False
    assert "pii" in rows["gliner"]["capabilities"]


def test_an_engine_whose_constructor_explodes_is_reported_not_raised(monkeypatch):
    from agentconnect.safety import registry as registry_mod

    def boom():
        raise RuntimeError("bad options")

    monkeypatch.setitem(registry_mod.KNOWN_ENGINES, "presidio", boom)
    config = SafetyConfig(engines={"presidio": EngineConfig(enabled=True)})
    registry = EngineRegistry(config)  # must not raise

    assert "presidio" not in registry
    row = next(r for r in registry.status() if r["engine"] == "presidio")
    assert row["constructed"] is False and row["available"] is False


# ========================================================= 3. the configuration
def test_from_dict_parses_the_documented_yaml_shape():
    config = SafetyConfig.from_dict({"safety": {
        "enabled": True,
        "engines": {
            "baseline": {"enabled": True},
            "detect_secrets": {"enabled": True, "use_entropy_plugins": True},
            "trufflehog": {"enabled": False, "executable": "trufflehog",
                           "timeout_seconds": 20, "allow_network_verification": False},
        },
    }})

    assert config.enabled is True
    assert config.enabled_engines() == ["baseline", "detect_secrets"]
    assert config.engine("detect_secrets").options == {"use_entropy_plugins": True}
    assert config.engine("trufflehog").options["timeout_seconds"] == 20


def test_an_unknown_engine_in_configuration_raises_rather_than_being_ignored():
    """A typo must not silently disable the engine an operator believes protects them."""
    with pytest.raises(ValueError, match="unknown safety engine 'detect_secrests'"):
        SafetyConfig.from_dict({"safety": {"engines": {"detect_secrests": {"enabled": True}}}})

    with pytest.raises(ValueError, match="unknown safety engine"):
        SafetyConfig.from_dict({"safety": {"surfaces": {ARTIFACT_INGEST: ["nope"]}}})


def test_the_default_configuration_enables_only_the_baseline():
    assert SafetyConfig().enabled_engines() == ["baseline"]
    assert SafetyConfig().engine("detect_secrets").enabled is False


def test_engine_options_reach_the_constructor():
    engine = build_engine("trufflehog", {"executable": "th", "timeout_seconds": 3,
                                         "allow_network_verification": True})
    assert engine.executable == "th"
    assert engine.timeout_seconds == 3.0
    assert engine.allow_network_verification is True


# ====================================================== 4. surface × engine
def test_policy_chooses_engines_by_capability_not_the_engine_itself():
    secrets_only = FakeEngine(name="s", capabilities=[Capability.secrets])
    repo_only = FakeEngine(name="r", capabilities=[Capability.repository_secrets])
    pipeline = pipeline_with(secrets_only, repo_only)

    # `context_output` does not want repository engines: a subprocess per recalled
    # memory item is a poor trade.
    assert [n for n, _ in pipeline.engines_for(CONTEXT_OUTPUT)] == ["s"]
    assert sorted(n for n, _ in pipeline.engines_for(ARTIFACT_INGEST)) == ["r", "s"]
    assert [n for n, _ in pipeline.engines_for(REPOSITORY_SCAN)] == ["r"]


def test_an_operator_can_override_the_engines_for_a_surface():
    a, b = FakeEngine(name="a"), FakeEngine(name="b")
    pipeline = pipeline_with(a, b, surfaces={ARTIFACT_INGEST: ["b"]})
    assert [n for n, _ in pipeline.engines_for(ARTIFACT_INGEST)] == ["b"]


def test_the_baseline_alone_still_protects_both_surfaces():
    pipeline = SafetyPipeline(SafetyConfig())
    for surface in (ARTIFACT_INGEST, CONTEXT_OUTPUT):
        result = pipeline.scan_text(f"key {AWS}", surface=surface)
        assert result.decision is Decision.redact
        assert AWS not in result.redacted_content


# ============================================================ 5. aggregation
def test_two_engines_finding_the_same_span_produce_one_redaction():
    """The acceptance example: baseline sees a generic token, detect-secrets names it
    a GitHub token. One safe redaction; both attributions kept."""
    findings = aggregate([
        secret_finding(engine="baseline", rule="secret.github_token", start=5, end=45),
        secret_finding(engine="detect_secrets", rule="detect_secrets.github_token",
                       start=5, end=45, confidence=0.9),
    ])

    assert len(findings) == 1
    also = findings[0].metadata["also_detected_by"]
    assert [entry["engine"] for entry in also] == ["detect_secrets"]


def test_aggregation_is_a_union_not_an_intersection():
    """An engine that found nothing has abstained, not vetoed."""
    quiet = FakeEngine(name="quiet", findings=[])
    loud = FakeEngine(name="loud", findings=[secret_finding(engine="loud")])
    result = pipeline_with(quiet, loud).scan_text(AWS, surface=ARTIFACT_INGEST)

    assert result.decision is Decision.redact
    assert [f.engine for f in result.findings] == ["loud"]


def test_the_strongest_severity_survives_a_merge():
    findings = aggregate([
        secret_finding(engine="a", risk=RiskLevel.medium, start=0, end=10),
        secret_finding(engine="b", risk=RiskLevel.high, start=2, end=8),
    ])
    assert len(findings) == 1
    assert findings[0].risk_level is RiskLevel.high


def test_a_merge_widens_to_cover_both_spans():
    """One engine sees `sk-ant-…`, another only `sk-…`. Redacting the narrower span
    would leave the rest of the credential in the text."""
    findings = aggregate([
        secret_finding(engine="a", start=0, end=6),
        secret_finding(engine="b", start=3, end=20),
    ])
    assert findings[0].span == (0, 20)


def test_findings_in_different_categories_never_merge():
    injection = Finding(rule_id="i", category=Category.prompt_injection,
                        risk_level=RiskLevel.high, message="m", start=0, end=10)
    assert len(aggregate([secret_finding(start=0, end=10), injection])) == 2


def test_engine_attribution_survives_into_the_result():
    engine = FakeEngine(name="fake", findings=[secret_finding(engine="fake")])
    result = pipeline_with(engine).scan_text(AWS, surface=ARTIFACT_INGEST)

    assert result.findings[0].engine == "fake"
    assert result.findings[0].engine_version == "9.9"
    assert result.findings[0].to_dict()["engine"] == "fake"
    assert "engine:fake" in result.labels


def test_a_finding_never_carries_the_matched_value_whatever_the_engine():
    engine = FakeEngine(name="fake", findings=[secret_finding(engine="fake")])
    result = pipeline_with(engine).scan_text(f"x {AWS}", surface=ARTIFACT_INGEST)
    assert AWS not in json.dumps([f.to_dict() for f in result.findings])


# ========================================================= 6. failure states
def test_a_required_engine_that_raises_fails_closed():
    engine = FakeEngine(name="req", raises=RuntimeError("engine broke"))
    result = pipeline_with(engine, required=["req"]).scan_text(CLEAN, surface=ARTIFACT_INGEST)

    assert result.decision is Decision.quarantine
    assert result.scanner_failed is True
    assert result.engines_failed == ["req"]
    assert any("failed" in w for w in result.warnings)


def test_an_optional_engine_that_raises_warns_and_is_never_clean():
    """The engines that ran still have their say — but a failure is not an `allow`."""
    broken = FakeEngine(name="opt", raises=RuntimeError("boom"))
    fine = FakeEngine(name="ok", findings=[])
    result = pipeline_with(broken, fine).scan_text(CLEAN, surface=ARTIFACT_INGEST)

    assert result.decision is not Decision.allow
    assert result.decision is Decision.warn
    assert result.scanner_failed is True
    assert "ok" in result.engines_run and "opt" in result.engines_failed


def test_an_optional_engine_failure_does_not_discard_another_engines_finding():
    broken = FakeEngine(name="opt", raises=RuntimeError("boom"))
    finder = FakeEngine(name="finder", findings=[secret_finding(engine="finder")])
    result = pipeline_with(broken, finder).scan_text(AWS, surface=ARTIFACT_INGEST)

    assert result.decision is Decision.redact  # the real finding still governs
    assert AWS not in result.redacted_content


def test_a_required_engine_that_is_unavailable_fails_closed():
    engine = FakeEngine(name="req", is_available=False)
    result = pipeline_with(engine, required=["req"]).scan_text(CLEAN, surface=ARTIFACT_INGEST)

    assert result.decision is Decision.quarantine
    assert result.engines_unavailable == ["req"]


def test_an_optional_engine_that_is_unavailable_warns_but_does_not_gate():
    """Not installed is not the same as broken: it never looked, and it never claimed to."""
    engine = FakeEngine(name="opt", is_available=False)
    result = pipeline_with(engine).scan_text(CLEAN, surface=ARTIFACT_INGEST)

    assert result.decision is Decision.allow
    assert result.scanner_failed is False
    assert any("enabled but unavailable" in w for w in result.warnings)


def test_the_five_engine_states_are_distinguishable():
    ok = FakeEngine(name="ok", findings=[])
    unavailable = FakeEngine(name="gone", is_available=False)
    failed = FakeEngine(name="broke", raises=RuntimeError("x"))
    timed_out = FakeEngine(name="slow", raises=TimeoutError("too slow"))
    result = pipeline_with(ok, unavailable, failed, timed_out).scan_text(
        CLEAN, surface=ARTIFACT_INGEST)

    states = {o.name: o.status for o in result.engines}
    assert states == {"ok": EngineStatus.ok, "gone": EngineStatus.unavailable,
                      "broke": EngineStatus.failed, "slow": EngineStatus.timeout}
    # `ok` found nothing. That is a result, not an absence.
    assert next(o for o in result.engines if o.name == "ok").looked is True


def test_an_available_check_that_raises_is_a_failure_not_an_absence():
    class Rude(FakeEngine):
        def available(self):
            raise RuntimeError("available() should never raise")

    result = pipeline_with(Rude(name="rude"), required=["rude"]).scan_text(
        CLEAN, surface=ARTIFACT_INGEST)
    assert result.decision is Decision.quarantine
    assert result.engines_failed == ["rude"]


def test_a_secret_that_cannot_be_located_is_withheld_not_falsely_redacted():
    """An engine can report a credential without a usable offset. Emitting a redaction
    marker then would announce the secret was handled while it stayed in the text."""
    engine = FakeEngine(name="spanless",
                        findings=[secret_finding(engine="spanless", start=0, end=0)])
    result = pipeline_with(engine).scan_text(f"x {AWS}", surface=ARTIFACT_INGEST)

    assert result.decision is Decision.quarantine
    assert AWS in result.redacted_content  # not rewritten...
    assert any("cannot be redacted" in w for w in result.warnings)  # ...and said so


# ================================================= 7. external tool base class
class FakeTool(ExternalToolEngine):
    name = "faketool"
    version = "external"
    capabilities = frozenset({Capability.secrets})
    executable = "faketool"

    def argv(self, target):
        return [sys.executable, "-c", "print('{}')"]

    def parse(self, stdout, text):
        return []


def test_an_external_tool_is_unavailable_when_its_binary_is_missing():
    engine = FakeTool(executable="definitely-not-on-path-12345")
    assert engine.available() is False  # and it did not raise


def test_an_external_tool_that_exceeds_its_timeout_raises_timeout(monkeypatch):
    class Slow(FakeTool):
        def argv(self, target):
            return [sys.executable, "-c", "import time; time.sleep(5)"]

    engine = Slow(executable=sys.executable, timeout_seconds=0.2)
    with pytest.raises(TimeoutError, match="exceeded"):
        engine.scan(EngineScanRequest(text="x", surface=ARTIFACT_INGEST))


def test_a_timeout_becomes_the_timeout_state_and_fails_closed_when_required():
    class Slow(FakeTool):
        def argv(self, target):
            return [sys.executable, "-c", "import time; time.sleep(5)"]

    engine = Slow(executable=sys.executable, timeout_seconds=0.2)
    result = pipeline_with(engine, required=["faketool"]).scan_text(
        CLEAN, surface=ARTIFACT_INGEST)

    assert result.engines[0].status is EngineStatus.timeout
    assert result.decision is Decision.quarantine


def test_json_lines_ignores_prose_and_blank_lines():
    records = ExternalToolEngine.json_lines(
        'not json\n\n{"a": 1}\n["not a dict"]\n{"b": 2}\n')
    assert records == [{"a": 1}, {"b": 2}]


def test_locate_returns_no_span_when_the_value_is_absent():
    assert ExternalToolEngine.locate("hello world", "zzz") == (0, 0)
    assert ExternalToolEngine.locate("a AKIA b", "AKIA") == (2, 6)


# ==================================================== 8. detect-secrets adapter
detect_secrets_installed = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["find_spec"]).find_spec("detect_secrets") is None,
    reason="detect-secrets not installed (pip install 'agentconnect-core[safety-secrets]')")


def test_detect_secrets_reports_unavailable_when_the_library_is_absent(monkeypatch):
    from agentconnect.safety.engines.detect_secrets import DetectSecretsEngine

    monkeypatch.setitem(sys.modules, "detect_secrets", None)
    monkeypatch.setitem(sys.modules, "detect_secrets.core.scan", None)
    engine = DetectSecretsEngine()
    assert engine.available() is False  # no import crash


def test_expand_to_token_recovers_a_credential_from_a_prefix_match():
    """detect-secrets' GitHub detector returns `secret_value == "ghp"`. Redacting that
    span masks three characters and leaves the token in the text."""
    from agentconnect.safety.engines.detect_secrets import expand_to_token

    line = "gh = ghp_abcdefghij1234\n"
    assert expand_to_token(line, 5, 8) == (5, 23)          # widened past the prefix
    # It stops at an assignment: the variable name is the part worth keeping.
    quoted = 'key="ghp_abcdefghij1234"'
    start, end = expand_to_token(quoted, 5, 8)
    assert quoted[start:end] == "ghp_abcdefghij1234"


@detect_secrets_installed
def test_detect_secrets_really_detects_a_key_through_the_adapter():
    """Guards the `default_settings()` context. Without it, `scan_line` yields nothing:
    no error, no warning, and an adapter that silently detects nothing forever."""
    from agentconnect.safety.engines.detect_secrets import DetectSecretsEngine

    engine = DetectSecretsEngine()
    assert engine.available() is True
    findings = engine.scan(EngineScanRequest(text=f"aws_key = {AWS}\n", surface=ARTIFACT_INGEST))

    rules = {f.rule_id for f in findings}
    assert "detect_secrets.aws_access_key" in rules
    aws = next(f for f in findings if f.rule_id == "detect_secrets.aws_access_key")
    assert f"aws_key = {AWS}\n"[aws.start:aws.end] == AWS
    assert engine.version != "unknown"


@detect_secrets_installed
def test_detect_secrets_suppresses_entropy_plugins_by_default():
    """`Base64HighEntropyString` fires on ordinary identifiers. Redacting on it would
    eat variable names and teach the operator to switch the engine off."""
    from agentconnect.safety.engines.detect_secrets import DetectSecretsEngine

    text = f"aws_key = {AWS}\n"
    quiet = DetectSecretsEngine().scan(EngineScanRequest(text=text, surface=ARTIFACT_INGEST))
    noisy = DetectSecretsEngine(use_entropy_plugins=True).scan(
        EngineScanRequest(text=text, surface=ARTIFACT_INGEST))

    assert not any("entropy" in f.rule_id for f in quiet)
    assert any("entropy" in f.rule_id for f in noisy)
    assert all(f.confidence < 0.9 for f in noisy if "entropy" in f.rule_id)


@detect_secrets_installed
def test_the_baseline_and_detect_secrets_together_redact_once():
    config = SafetyConfig(engines={
        "baseline": EngineConfig(enabled=True, required=True),
        "detect_secrets": EngineConfig(enabled=True),
    })
    result = SafetyPipeline(config).scan_text(f"aws_key = {AWS}", surface=ARTIFACT_INGEST)

    assert result.redacted_content.count("[REDACTED") == 1
    assert AWS not in result.redacted_content
    assert sorted(result.engines_run) == ["baseline", "detect_secrets"]


# ================================================ 9. trufflehog & gitleaks
TRUFFLEHOG_JSON = (
    '{"DetectorName":"AWS","Verified":false,"Raw":"' + AWS + '"}\n'
    'garbage not json\n'
    '{"DetectorName":"Github","Verified":true,"Raw":"ghp_zzz"}\n'
)
GITLEAKS_JSON = json.dumps([
    {"RuleID": "aws-access-token", "Secret": AWS, "Match": f"key = {AWS}"},
    {"RuleID": "generic-api-key", "Secret": "not-in-the-text"},
])


def test_trufflehog_never_verifies_over_the_network_by_default():
    """Verification authenticates the candidate credential against its service. For a
    scanner meant to stop credentials escaping, that is exfiltration by the guard."""
    from agentconnect.safety.engines.trufflehog import TruffleHogEngine
    from pathlib import Path

    argv = TruffleHogEngine().argv(Path("/tmp/x"))
    assert "--no-verification" in argv
    assert "--results=verified,unknown,unverified" in argv  # else every hit is hidden

    opted_in = TruffleHogEngine(allow_network_verification=True).argv(Path("/tmp/x"))
    assert "--no-verification" not in opted_in


def test_trufflehog_parses_json_lines_into_normalized_findings():
    from agentconnect.safety.engines.trufflehog import TruffleHogEngine

    text = f"key {AWS} end"
    findings = TruffleHogEngine().parse(TRUFFLEHOG_JSON, text)

    assert [f.rule_id for f in findings] == ["trufflehog.aws", "trufflehog.github"]
    assert findings[0].span == (4, 4 + len(AWS))
    assert findings[0].engine == "trufflehog"
    assert findings[0].category is Category.secret
    assert findings[1].span == (0, 0)          # value not present in the text
    assert findings[1].metadata["verified"] is True
    assert AWS not in json.dumps([f.to_dict() for f in findings])


def test_gitleaks_parses_its_json_array_and_prefers_the_secret_over_the_match():
    """`Match` is the whole line fragment. Masking it would eat the assignment's
    left-hand side; `Secret` is the value."""
    from agentconnect.safety.engines.gitleaks import GitleaksEngine

    text = f"key = {AWS}"
    findings = GitleaksEngine().parse(GITLEAKS_JSON, text)

    assert findings[0].rule_id == "gitleaks.aws-access-token"
    assert text[findings[0].start:findings[0].end] == AWS
    assert findings[1].span == (0, 0)  # unlocatable -> the pipeline will quarantine


def test_gitleaks_tolerates_an_empty_or_broken_report():
    from agentconnect.safety.engines.gitleaks import GitleaksEngine

    assert GitleaksEngine().parse("", "x") == []
    assert GitleaksEngine().parse("not json", "x") == []
    assert GitleaksEngine().parse('{"not": "a list"}', "x") == []


@pytest.mark.skipif(shutil.which("trufflehog") is None, reason="trufflehog not installed")
def test_trufflehog_binary_runs_offline_and_returns_a_list():
    from agentconnect.safety.engines.trufflehog import TruffleHogEngine

    engine = TruffleHogEngine(timeout_seconds=60)
    assert engine.available() is True
    findings = engine.scan(EngineScanRequest(text=f"key {AWS}", surface=ARTIFACT_INGEST))
    assert isinstance(findings, list)  # fake keys are not detected; it must not raise


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_binary_detects_an_assignment_and_places_its_span():
    from agentconnect.safety.engines.gitleaks import GitleaksEngine

    engine = GitleaksEngine(timeout_seconds=60)
    assert engine.available() is True
    text = f'api_key = "{AWS}"\n'
    findings = engine.scan(EngineScanRequest(text=text, surface=ARTIFACT_INGEST))

    assert findings, "gitleaks should flag a secret-shaped assignment"
    located = [f for f in findings if f.has_span]
    assert located, "gitleaks findings must be locatable, or they cannot be redacted"
    assert AWS in text[located[0].start:located[0].end]


# ======================================= 10. presidio, gliner, prompt_guard
# Adapters exist and are exercised through fakes. Neither library is installed in
# this gate, so `available()` is False and no real inference is asserted.

def test_presidio_is_unavailable_without_the_library():
    from agentconnect.safety.engines.presidio import PresidioEngine

    assert PresidioEngine().available() is False
    assert Capability.pii in PresidioEngine().capabilities


def test_presidio_normalizes_analyzer_results():
    from agentconnect.safety.engines.presidio import PresidioEngine

    class Result:
        def __init__(self, entity_type, start, end, score):
            self.entity_type, self.start, self.end, self.score = entity_type, start, end, score

    class Analyzer:
        def analyze(self, text, language, entities):
            return [Result("US_SSN", 0, 11, 0.95), Result("PERSON", 12, 15, 0.4)]

    engine = PresidioEngine()
    engine._analyzer = Analyzer()
    findings = engine.scan(EngineScanRequest(text="123-45-6789 Bob", surface=ARTIFACT_INGEST))

    assert [f.rule_id for f in findings] == ["presidio.us_ssn"]  # PERSON below threshold
    assert findings[0].risk_level is RiskLevel.high
    assert findings[0].category is Category.pii
    assert findings[0].confidence == 0.95


def test_gliner_refuses_to_run_without_a_pinned_model():
    """An unpinned model would download at scan time, inside a managed agent run."""
    from agentconnect.safety.engines.gliner import GlinerEngine

    assert GlinerEngine().available() is False
    assert GlinerEngine(model="urchade/gliner_small").local_files_only is True


def test_gliner_normalizes_predicted_entities():
    from agentconnect.safety.engines.gliner import GlinerEngine

    class Model:
        def predict_entities(self, text, labels, threshold):
            return [{"label": "person", "start": 0, "end": 3, "score": 0.8}]

    engine = GlinerEngine(model="x")
    engine._model = Model()
    findings = engine.scan(EngineScanRequest(text="Bob", surface=ARTIFACT_INGEST))

    assert findings[0].rule_id == "gliner.person"
    assert findings[0].category is Category.pii


def test_prompt_guard_refuses_an_unpinned_model():
    """A verdict that changes when a remote weight changes is not a verdict."""
    from agentconnect.safety.engines.prompt_guard import PromptGuardEngine

    assert PromptGuardEngine().available() is False
    assert PromptGuardEngine().version == "unpinned"


def test_prompt_guard_produces_a_spanless_injection_finding(monkeypatch):
    from agentconnect.safety.engines.prompt_guard import PromptGuardEngine

    engine = PromptGuardEngine(model="pinned/model")
    monkeypatch.setattr(engine, "score", lambda text: 0.95)
    findings = engine.scan(EngineScanRequest(text="ignore previous", surface=CONTEXT_OUTPUT))

    assert findings[0].category is Category.prompt_injection
    assert findings[0].risk_level is RiskLevel.high
    assert findings[0].has_span is False  # a whole-text score cannot be redacted
    assert findings[0].confidence == 0.95


def test_a_spanless_injection_finding_withholds_rather_than_redacts():
    from agentconnect.safety.engines.prompt_guard import PromptGuardEngine

    engine = PromptGuardEngine(model="pinned/model")
    engine.available = lambda: True
    engine.score = lambda text: 0.99
    result = pipeline_with(engine).scan_text("anything", surface=CONTEXT_OUTPUT)

    assert result.decision is Decision.quarantine
    assert result.redacted_content == "anything"  # nothing to mask, nothing masked


# ============================================ 11. batch scanning through engines
def test_a_batch_scan_preserves_identity_across_engines():
    engine = FakeEngine(name="fake", findings=[])
    batch = pipeline_with(engine).scan_items(
        [SafetyItem(id="a", text=CLEAN), SafetyItem(id="b", text=CLEAN)],
        policy=CONTEXT_OUTPUT)
    assert sorted(batch.results) == ["a", "b"]


def test_an_engine_failure_inside_a_batch_never_reads_as_clean():
    engine = FakeEngine(name="req", raises=RuntimeError("boom"))
    batch = pipeline_with(engine, required=["req"]).scan_items(
        [SafetyItem(id="a", text=CLEAN)], policy=CONTEXT_OUTPUT)

    assert batch.results["a"].decision is Decision.quarantine
    assert batch.withheld_ids == ["a"]
    assert any("withheld" in w for w in batch.warnings())


# ================================================ 12. the operator's config seam
def test_no_safety_config_file_means_the_default_pipeline(tmp_path, monkeypatch):
    from agentconnect.core.bootstrap import safety_from_env

    monkeypatch.setenv("AGENTCONNECT_SAFETY_CONFIG", str(tmp_path / "absent.yaml"))
    assert safety_from_env() is None  # None == baseline only


def test_a_safety_config_file_builds_the_configured_pipeline(tmp_path, monkeypatch):
    config = tmp_path / "safety.yaml"
    config.write_text(
        "safety:\n  enabled: true\n  engines:\n"
        "    baseline: {enabled: true, required: true}\n"
        "    gitleaks: {enabled: true, timeout_seconds: 5}\n")
    monkeypatch.setenv("AGENTCONNECT_SAFETY_CONFIG", str(config))

    from agentconnect.core.bootstrap import safety_from_env

    pipeline = safety_from_env()
    assert sorted(pipeline.config.enabled_engines()) == ["baseline", "gitleaks"]
    assert pipeline.registry.get("gitleaks").timeout_seconds == 5.0


def test_a_typo_in_the_safety_config_stops_startup(tmp_path, monkeypatch):
    """Memory degrades to "off" when its YAML is unreadable; safety must not. An
    operator who wrote `detect_secrests:` believes an engine is running."""
    config = tmp_path / "safety.yaml"
    config.write_text("safety:\n  engines:\n    detect_secrests: {enabled: true}\n")
    monkeypatch.setenv("AGENTCONNECT_SAFETY_CONFIG", str(config))

    from agentconnect.core.bootstrap import safety_from_env

    with pytest.raises(ValueError, match="unknown safety engine"):
        safety_from_env()
