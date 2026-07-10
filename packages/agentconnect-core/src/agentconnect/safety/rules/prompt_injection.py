"""Text that addresses the agent rather than informing it.

The premise: a stored artifact and a recalled memory item are *data*. When data
starts issuing instructions — "ignore previous instructions", "reveal the system
prompt" — it is trying to be a prompt. It may be a quotation, a test fixture, or
a security write-up, which is why most of these are `medium` and land on `warn`
rather than on `quarantine`. Only the two that exist almost exclusively to hijack
an agent are `high`.

These rules are deliberately literal. A cleverer classifier would be less
predictable, and an unpredictable safety layer gets turned off.
"""

from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel

PATTERNS: tuple[tuple[str, re.Pattern[str], RiskLevel, str], ...] = (
    ("injection.ignore_previous",
     re.compile(r"(?i)\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|directions?|rules?)"),
     RiskLevel.high, "Attempts to override prior instructions."),
    ("injection.disregard_previous",
     re.compile(r"(?i)\b(?:disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|prompts?|directions?|rules?)"),
     RiskLevel.high, "Attempts to override prior instructions."),
    ("injection.reveal_system_prompt",
     re.compile(r"(?i)\b(?:reveal|show|print|repeat|output|disclose)\s+"
                r"(?:me\s+)?(?:the\s+|your\s+)?system\s+prompt"),
     RiskLevel.high, "Attempts to extract the system prompt."),
    ("injection.new_instructions",
     re.compile(r"(?i)\b(?:new|updated|revised)\s+instructions?\s*:"),
     RiskLevel.medium, "Presents itself as a new instruction block."),
    ("injection.you_are_now",
     re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\b"),
     RiskLevel.medium, "Attempts to reassign the agent's role."),
    ("injection.role_marker",
     re.compile(r"(?im)^\s*(?:system|assistant)\s*:\s*\S"),
     RiskLevel.medium, "Impersonates a conversation role marker."),
    ("injection.do_not_tell",
     re.compile(r"(?i)\bdo\s+not\s+(?:tell|inform|mention\s+(?:this\s+)?to)\s+"
                r"(?:the\s+)?(?:user|operator|human)"),
     RiskLevel.high, "Instructs the agent to conceal its actions."),
)


def find(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule_id, pattern, risk, message in PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(
                rule_id=rule_id, category=Category.prompt_injection, risk_level=risk,
                message=message, start=match.start(), end=match.end(),
            ))
    return findings
