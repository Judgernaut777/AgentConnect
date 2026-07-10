"""What a finding *means*, per surface — and which engines run there.

AgentConnect owns this file. Engines detect; policy decides. A third-party tool
never learns what a task is, whether an artifact persists, or whether an audit
passes. It is handed text and returns findings.

The same finding warrants different handling depending on where it was found. A
probable secret in an artifact is redacted and stored, because the artifact is
evidence and the operator needs to know a credential was committed. The same secret
in a recalled memory item is redacted before it reaches the agent, because nobody
needs it there at all.

Policies are data. A new surface is a new table entry, not a new code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Capability, Category, Decision, RiskLevel

#: The two surfaces phase 1 protects.
ARTIFACT_INGEST = "artifact_ingest"
CONTEXT_OUTPUT = "context_output"

#: Named now because they appear in the design and in `docs/SAFETY.md`. They have
#: no table yet, and `policy()` refuses an unknown name rather than guessing.
SUBTASK_INSTRUCTION = "subtask_instruction"
REVIEW_INPUT = "review_input"
ATTEMPT_DECISION_NOTES = "attempt_decision_notes"
#: A file or directory scan, not a text scan. Only engines that claim
#: `repository_secrets` are useful here.
REPOSITORY_SCAN = "repository_scan"


@dataclass(frozen=True)
class Policy:
    name: str
    #: `(category, risk_level) -> decision`. A pair absent from the map is `allow`.
    rules: dict[tuple[Category, RiskLevel], Decision]
    #: What a *required* engine failing means here. Never `allow`, at any surface.
    on_scanner_error: Decision
    #: What an *optional* engine failing means. Never `allow` either — an engine
    #: that broke did not find the content clean — but it does not withhold on its
    #: own, because the engines that did run still have their say.
    on_optional_engine_error: Decision = Decision.warn
    #: What this surface wants detected. Engines are chosen by capability, so
    #: adding an engine never means editing a surface.
    capabilities: frozenset[Capability] = frozenset()


_ARTIFACT_RULES: dict[tuple[Category, RiskLevel], Decision] = {
    # A credential in stored evidence: keep the artifact, remove the credential.
    (Category.secret, RiskLevel.high): Decision.redact,
    (Category.secret, RiskLevel.medium): Decision.redact,

    (Category.pii, RiskLevel.high): Decision.redact,
    (Category.pii, RiskLevel.medium): Decision.redact,
    (Category.pii, RiskLevel.low): Decision.warn,

    # Injection text in an artifact is *not* quarantined on ingest. An artifact is
    # a record of what a worker produced, and a security write-up quoting "ignore
    # previous instructions" is a legitimate artifact. It is labeled here; the
    # `context_output` policy is what stops it reaching an agent.
    (Category.prompt_injection, RiskLevel.high): Decision.warn,
    (Category.prompt_injection, RiskLevel.medium): Decision.warn,

    (Category.tool_instruction, RiskLevel.high): Decision.quarantine,
    (Category.tool_instruction, RiskLevel.medium): Decision.warn,

    (Category.encoding, RiskLevel.low): Decision.warn,
}

_CONTEXT_RULES: dict[tuple[Category, RiskLevel], Decision] = {
    # Nothing an agent needs to read contains a live credential.
    (Category.secret, RiskLevel.high): Decision.redact,
    (Category.secret, RiskLevel.medium): Decision.redact,

    (Category.pii, RiskLevel.high): Decision.redact,
    (Category.pii, RiskLevel.medium): Decision.redact,
    (Category.pii, RiskLevel.low): Decision.warn,

    # This is the surface injection exists to attack: text is about to be handed to
    # an agent as context. High-confidence injection never arrives.
    (Category.prompt_injection, RiskLevel.high): Decision.quarantine,
    (Category.prompt_injection, RiskLevel.medium): Decision.warn,

    (Category.tool_instruction, RiskLevel.high): Decision.quarantine,
    (Category.tool_instruction, RiskLevel.medium): Decision.warn,

    (Category.encoding, RiskLevel.low): Decision.warn,
}

_REPOSITORY_RULES: dict[tuple[Category, RiskLevel], Decision] = {
    (Category.secret, RiskLevel.high): Decision.block,
    (Category.secret, RiskLevel.medium): Decision.warn,
}

#: Artifacts are files: worth a subprocess, so the repository-secret engines apply.
#: Context items are short prose recalled in bulk — a subprocess per item is a poor
#: trade, and no external tool is selected there by default.
_ARTIFACT_CAPABILITIES = frozenset({
    Capability.secrets, Capability.repository_secrets, Capability.pii,
    Capability.prompt_injection, Capability.tool_control, Capability.encoded_content,
})
_CONTEXT_CAPABILITIES = frozenset({
    Capability.secrets, Capability.pii, Capability.prompt_injection,
    Capability.tool_control, Capability.encoded_content,
})

POLICIES: dict[str, Policy] = {
    ARTIFACT_INGEST: Policy(
        ARTIFACT_INGEST, _ARTIFACT_RULES,
        on_scanner_error=Decision.quarantine,
        capabilities=_ARTIFACT_CAPABILITIES),
    # Withheld, not returned unscanned. A context item that could not be scanned is
    # the one item you least want to hand an agent unread.
    CONTEXT_OUTPUT: Policy(
        CONTEXT_OUTPUT, _CONTEXT_RULES,
        on_scanner_error=Decision.quarantine,
        capabilities=_CONTEXT_CAPABILITIES),
    REPOSITORY_SCAN: Policy(
        REPOSITORY_SCAN, _REPOSITORY_RULES,
        on_scanner_error=Decision.block,
        capabilities=frozenset({Capability.repository_secrets})),
}


def policy(name: str) -> Policy:
    try:
        return POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown safety policy {name!r}; known: {', '.join(sorted(POLICIES))}"
        ) from None
