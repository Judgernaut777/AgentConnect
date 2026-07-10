"""Types for AgentConnect-local safety scanning.

This is a **content** layer. It reads text at surfaces AgentConnect owns and
decides whether that text may be stored as evidence or handed to an agent. It is
not a sandbox: it cannot stop a process from opening the SQLite ledger, editing a
file, or reading its own environment, and it never claims that scanned content is
*true* — only that it does not obviously carry a credential or an instruction
aimed at the agent reading it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: Bumped whenever a rule changes what it matches or a policy changes a mapping.
#: Stored on every scanned artifact, so a finding can be read against the rules
#: that produced it rather than the rules that happen to exist today.
POLICY_VERSION = "1"


class Decision(str, Enum):
    """What the caller must do. Ordered: later decisions subsume earlier ones."""

    allow = "allow"
    warn = "warn"
    redact = "redact"
    quarantine = "quarantine"
    block = "block"


#: `max()` over a scan's decisions is the scan's decision. Explicit, because the
#: enum's own ordering is alphabetical and would put `block` before `warn`.
_SEVERITY_OF_DECISION = {
    Decision.allow: 0, Decision.warn: 1, Decision.redact: 2,
    Decision.quarantine: 3, Decision.block: 4,
}


def strongest(decisions: list[Decision]) -> Decision:
    return max(decisions, key=_SEVERITY_OF_DECISION.__getitem__, default=Decision.allow)


class RiskLevel(str, Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


_SEVERITY_OF_RISK = {RiskLevel.none: 0, RiskLevel.low: 1, RiskLevel.medium: 2, RiskLevel.high: 3}


def highest(levels: list[RiskLevel]) -> RiskLevel:
    return max(levels, key=_SEVERITY_OF_RISK.__getitem__, default=RiskLevel.none)


class Category(str, Enum):
    secret = "secret"
    prompt_injection = "prompt_injection"
    tool_instruction = "tool_instruction"
    encoding = "encoding"
    #: A rule raised. Never `allow`: a scanner that failed has not said the
    #: content is clean, and must not be read as if it had.
    scanner_error = "scanner_error"


@dataclass(frozen=True)
class Finding:
    """One match. It deliberately does **not** carry the matched text.

    A finding travels into artifact metadata, into logs, and into a context pack's
    warnings. Putting the secret in it would move the secret to three new places
    while announcing that it had been removed from one.
    """

    rule_id: str
    category: Category
    risk_level: RiskLevel
    message: str
    #: Half-open `[start, end)` into the scanned text. Redaction consumes these.
    start: int = 0
    end: int = 0

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "category": self.category.value,
                "risk_level": self.risk_level.value, "message": self.message,
                "start": self.start, "end": self.end}


@dataclass
class SafetyResult:
    decision: Decision = Decision.allow
    risk_level: RiskLevel = RiskLevel.none
    findings: list[Finding] = field(default_factory=list)
    #: The text the caller should store or hand on. Equal to the input when
    #: nothing was redacted — never `None`, so a caller cannot use it by accident
    #: while believing it used the original.
    redacted_content: str = ""
    labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION
    #: True when a rule raised. The decision is already fail-closed; this exists so
    #: a caller can tell "we looked and found nothing" from "we could not look."
    scanner_failed: bool = False

    @property
    def redacted(self) -> bool:
        return any(f.category is Category.secret for f in self.findings) \
            and self.decision is Decision.redact

    @property
    def withheld(self) -> bool:
        return self.decision in (Decision.quarantine, Decision.block)

    def to_metadata(self) -> dict[str, Any]:
        """The `safety_*` block stored alongside an artifact."""
        return {
            "safety_decision": self.decision.value,
            "safety_risk_level": self.risk_level.value,
            "safety_findings": [f.to_dict() for f in self.findings],
            "safety_policy_version": self.policy_version,
            "safety_redacted": self.redacted,
            "safety_warnings": list(self.warnings),
            "safety_scanner_failed": self.scanner_failed,
        }


@dataclass
class SafetyItem:
    """A unit of a batch scan. `id` is the caller's, and comes back unchanged."""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SafetyBatchResult:
    #: Keyed by `SafetyItem.id`. Every input id appears exactly once, so a caller
    #: can never lose track of which item was withheld.
    results: dict[str, SafetyResult] = field(default_factory=dict)
    policy_version: str = POLICY_VERSION

    def decision_for(self, item_id: str) -> Decision:
        result = self.results.get(item_id)
        return result.decision if result else Decision.allow

    @property
    def redacted_ids(self) -> list[str]:
        return [i for i, r in self.results.items() if r.redacted]

    @property
    def withheld_ids(self) -> list[str]:
        return [i for i, r in self.results.items() if r.withheld]

    def warnings(self) -> list[str]:
        """Pack-level warnings. Silence about a withheld item is the bug this
        prevents: a shorter context pack looks exactly like a quiet one."""
        lines: list[str] = []
        redacted, withheld = len(self.redacted_ids), len(self.withheld_ids)
        if redacted:
            lines.append(f"{redacted} context item{'s were' if redacted > 1 else ' was'} "
                         f"redacted by AgentConnect safety scanning.")
        if withheld:
            lines.append(f"{withheld} context item{'s were' if withheld > 1 else ' was'} "
                         f"withheld by AgentConnect safety scanning.")
        failed = [i for i, r in self.results.items() if r.scanner_failed]
        if failed:
            lines.append(f"{len(failed)} context item{'s' if len(failed) > 1 else ''} "
                         f"could not be scanned and {'were' if len(failed) > 1 else 'was'} "
                         f"withheld; safety scanning failed.")
        return lines
