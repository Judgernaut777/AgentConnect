"""The scan entry points: `scan_text` and `scan_items`.

One invariant governs this file. **A scanner that failed has not found the content
clean.** Every rule runs inside its own `try`, and a rule that raises produces a
`scanner_error` finding whose decision comes from the policy — never `allow`. The
alternative, a broad `except` that returns an empty result, is the shape of bug
this project has already been bitten by twice: a silent degradation that reads,
downstream, as a clean bill of health.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable

from .models import (
    POLICY_VERSION,
    Category,
    Decision,
    Finding,
    RiskLevel,
    SafetyBatchResult,
    SafetyItem,
    SafetyResult,
    highest,
    strongest,
)
from .policies import Policy, policy as _policy
from .redaction import redact
from .rules import encoding, prompt_injection, secrets, tool_instructions

_log = logging.getLogger(__name__)

#: `(name, find)`. A rule is a pure function of text; it holds no state and reads
#: no configuration, so a finding is reproducible from the text alone.
RULESETS: tuple[tuple[str, Callable[[str], list[Finding]]], ...] = (
    ("secrets", secrets.find),
    ("prompt_injection", prompt_injection.find),
    ("tool_instructions", tool_instructions.find),
    ("encoding", encoding.find),
)


def _run_rules(text: str) -> tuple[list[Finding], list[str], bool]:
    findings: list[Finding] = []
    warnings: list[str] = []
    failed = False
    for name, find in RULESETS:
        try:
            findings.extend(find(text))
        except Exception as exc:  # noqa: BLE001 — a broken rule must not read as clean
            failed = True
            _log.warning("safety ruleset %s failed: %s", name, exc)
            warnings.append(f"safety ruleset {name!r} failed: {exc}")
            findings.append(Finding(
                rule_id=f"scanner.{name}.error", category=Category.scanner_error,
                risk_level=RiskLevel.high,
                message=f"Ruleset {name!r} raised; content was not fully scanned.",
            ))
    return findings, warnings, failed


def _decide(findings: list[Finding], pol: Policy) -> tuple[Decision, RiskLevel]:
    decisions: list[Decision] = []
    for finding in findings:
        if finding.category is Category.scanner_error:
            decisions.append(pol.on_scanner_error)
        else:
            decisions.append(pol.decide(finding.category, finding.risk_level))
    risks = [f.risk_level for f in findings]
    return strongest(decisions), highest(risks)


def _labels(findings: list[Finding]) -> list[str]:
    seen: dict[str, None] = {}
    for finding in findings:
        seen.setdefault(f"safety:{finding.category.value}", None)
    return list(seen)


def scan_text(content: str, *, surface: str, policy: str) -> SafetyResult:
    """Scan one piece of text under a named policy.

    `surface` is recorded for the caller's logs and warnings; `policy` selects the
    decision table. They are separate because a future surface may reuse an
    existing policy, and conflating them would force a new table for each caller.
    """
    pol = _policy(policy)
    text = content or ""
    findings, warnings, failed = _run_rules(text)

    if not findings:
        return SafetyResult(decision=Decision.allow, risk_level=RiskLevel.none,
                            redacted_content=text, policy_version=POLICY_VERSION)

    decision, risk = _decide(findings, pol)

    # Redact only what the policy actually decided to redact. A `warn`-level
    # encoding blob keeps its text; a secret does not, regardless of whether the
    # overall decision escalated past `redact` to `quarantine`.
    to_redact = [
        f for f in findings
        if f.category is not Category.scanner_error
        and pol.decide(f.category, f.risk_level) is Decision.redact
    ]
    body = redact(text, to_redact) if to_redact else text

    if failed:
        warnings.append(
            f"{surface}: safety scanning failed; content treated as {decision.value}."
        )

    return SafetyResult(
        decision=decision, risk_level=risk, findings=findings, redacted_content=body,
        labels=_labels(findings), warnings=warnings, policy_version=POLICY_VERSION,
        scanner_failed=failed,
    )


def scan_items(items: Iterable[SafetyItem], *, policy: str) -> SafetyBatchResult:
    """Scan many items, preserving each caller-supplied id.

    Identity is the point: the caller has to know *which* item was withheld, or the
    only honest thing it can report is that the pack got shorter.
    """
    batch = SafetyBatchResult(policy_version=POLICY_VERSION)
    for item in items:
        batch.results[item.id] = scan_text(
            item.text, surface=policy, policy=policy,
        )
    return batch
