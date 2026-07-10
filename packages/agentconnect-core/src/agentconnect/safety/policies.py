"""What a finding *means*, per surface.

The same finding warrants different handling depending on where it was found. A
probable secret in an artifact is redacted and stored, because the artifact is
evidence and the operator needs to know a credential was committed. The same
secret in a recalled memory item is redacted before it reaches the agent, because
nobody needs it there at all.

Policies are data. A new surface is a new table entry, not a new code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Category, Decision, RiskLevel

#: The two surfaces phase 1 protects.
ARTIFACT_INGEST = "artifact_ingest"
CONTEXT_OUTPUT = "context_output"

#: Named now because they appear in the design and in `docs/SAFETY.md`. They have
#: no table yet, and `policy()` refuses an unknown name rather than guessing.
SUBTASK_INSTRUCTION = "subtask_instruction"
REVIEW_INPUT = "review_input"
ATTEMPT_DECISION_NOTES = "attempt_decision_notes"


@dataclass(frozen=True)
class Policy:
    name: str
    #: `(category, risk_level) -> decision`. A pair absent from the map is `allow`.
    rules: dict[tuple[Category, RiskLevel], Decision]
    #: What a rule that *raised* means here. Never `allow`, at any surface.
    on_scanner_error: Decision

    def decide(self, category: Category, risk: RiskLevel) -> Decision:
        return self.rules.get((category, risk), Decision.allow)


_ARTIFACT_RULES: dict[tuple[Category, RiskLevel], Decision] = {
    # A credential in stored evidence: keep the artifact, remove the credential.
    (Category.secret, RiskLevel.high): Decision.redact,
    (Category.secret, RiskLevel.medium): Decision.redact,

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

    # This is the surface injection exists to attack: text is about to be handed to
    # an agent as context. High-confidence injection never arrives.
    (Category.prompt_injection, RiskLevel.high): Decision.quarantine,
    (Category.prompt_injection, RiskLevel.medium): Decision.warn,

    (Category.tool_instruction, RiskLevel.high): Decision.quarantine,
    (Category.tool_instruction, RiskLevel.medium): Decision.warn,

    (Category.encoding, RiskLevel.low): Decision.warn,
}

POLICIES: dict[str, Policy] = {
    ARTIFACT_INGEST: Policy(ARTIFACT_INGEST, _ARTIFACT_RULES,
                            on_scanner_error=Decision.quarantine),
    # Withheld, not returned unscanned. A context item that could not be scanned is
    # the one item you least want to hand an agent unread.
    CONTEXT_OUTPUT: Policy(CONTEXT_OUTPUT, _CONTEXT_RULES,
                           on_scanner_error=Decision.quarantine),
}


def policy(name: str) -> Policy:
    try:
        return POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown safety policy {name!r}; known: {', '.join(sorted(POLICIES))}"
        ) from None
