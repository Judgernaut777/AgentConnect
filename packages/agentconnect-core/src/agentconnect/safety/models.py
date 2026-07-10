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
    pii = "pii"
    prompt_injection = "prompt_injection"
    tool_instruction = "tool_instruction"
    encoding = "encoding"
    #: A rule or an engine raised. Never `allow`: a scanner that failed has not
    #: said the content is clean, and must not be read as if it had.
    scanner_error = "scanner_error"


class Capability(str, Enum):
    """What an engine claims it can detect. Policy selects engines by capability,
    so adding an engine never means editing a surface."""

    secrets = "secrets"
    pii = "pii"
    prompt_injection = "prompt_injection"
    tool_control = "tool_control"
    encoded_content = "encoded_content"
    repository_secrets = "repository_secrets"


#: The category an engine's finding lands in, per capability it claims.
CATEGORY_OF_CAPABILITY: dict[Capability, Category] = {
    Capability.secrets: Category.secret,
    Capability.repository_secrets: Category.secret,
    Capability.pii: Category.pii,
    Capability.prompt_injection: Category.prompt_injection,
    Capability.tool_control: Category.tool_instruction,
    Capability.encoded_content: Category.encoding,
}


class EngineStatus(str, Enum):
    """Five distinct states. They are not interchangeable, and collapsing any two
    of them is how a scanner starts reporting unread content as clean.

    * `ok` — the engine ran. It may have found nothing; that is a *result*.
    * `unavailable` — not installed, no binary, no model. It never looked.
    * `failed` — present, and it raised. It looked and we do not know what it saw.
    * `skipped` — disabled by configuration, or lacks a capability this surface wants.
    * `timeout` — an external tool exceeded its budget. A `failed` with a cause.
    """

    ok = "ok"
    unavailable = "unavailable"
    failed = "failed"
    skipped = "skipped"
    timeout = "timeout"


@dataclass(frozen=True)
class Finding:
    """One normalized detection, whichever engine produced it.

    It deliberately does **not** carry the matched text. A finding travels into
    artifact metadata, into logs, and into a context pack's warnings. Putting the
    secret in it would move the secret to three new places while announcing that it
    had been removed from one. Third-party engines hand us their raw match; the
    adapter converts it to a span and drops the value.
    """

    rule_id: str
    category: Category
    risk_level: RiskLevel
    message: str
    #: Half-open `[start, end)` into the scanned text. Redaction consumes these.
    #: `(0, 0)` means "no span": a whole-text classifier score, for instance, which
    #: can be warned about but never redacted.
    start: int = 0
    end: int = 0
    #: Which engine said so, and at what version. Attribution survives aggregation:
    #: two engines agreeing is evidence, and one engine's false positive is a bug
    #: report for that engine, not for the pipeline.
    engine: str = "baseline"
    engine_version: str = ""
    #: `[0, 1]`. Deterministic rules assert 1.0; a classifier reports its score.
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    @property
    def has_span(self) -> bool:
        return self.end > self.start

    @property
    def severity(self) -> str:
        """The handoff's name for `risk_level`. Same value, one vocabulary."""
        return self.risk_level.value

    @property
    def rule(self) -> str:
        return self.rule_id

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "category": self.category.value,
                "risk_level": self.risk_level.value, "message": self.message,
                "start": self.start, "end": self.end,
                "engine": self.engine, "engine_version": self.engine_version,
                "confidence": self.confidence,
                **({"metadata": self.metadata} if self.metadata else {})}


@dataclass
class EngineOutcome:
    """What one engine did on one scan. The pipeline reasons over these, not over
    exceptions, so `required` failure and `optional` absence stay distinguishable."""

    name: str
    status: EngineStatus
    required: bool = False
    findings: list[Finding] = field(default_factory=list)
    error: str = ""
    version: str = ""

    @property
    def looked(self) -> bool:
        """Did this engine actually read the content? `ok` alone qualifies."""
        return self.status is EngineStatus.ok

    @property
    def broke(self) -> bool:
        return self.status in (EngineStatus.failed, EngineStatus.timeout)

    def to_dict(self) -> dict[str, Any]:
        out = {"engine": self.name, "status": self.status.value,
               "required": self.required, "findings": len(self.findings)}
        if self.error:
            out["error"] = self.error
        if self.version:
            out["version"] = self.version
        return out


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
    #: True when a rule or engine raised. The decision is already fail-closed; this
    #: exists so a caller can tell "we looked and found nothing" from "we could not
    #: look."
    scanner_failed: bool = False
    #: One entry per engine the surface's policy asked for.
    engines: list[EngineOutcome] = field(default_factory=list)

    @property
    def engines_run(self) -> list[str]:
        return [e.name for e in self.engines if e.looked]

    @property
    def engines_unavailable(self) -> list[str]:
        return [e.name for e in self.engines if e.status is EngineStatus.unavailable]

    @property
    def engines_failed(self) -> list[str]:
        return [e.name for e in self.engines if e.broke]

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
            "safety_engines": [e.to_dict() for e in self.engines],
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
