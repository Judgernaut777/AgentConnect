"""Probable credentials.

Patterns, not entropy heuristics. A regex that matches `AKIA` + 16 uppercase
characters is wrong in ways a reviewer can see and fix; an entropy threshold is
wrong in ways nobody can explain to the person whose artifact got mangled.

Every rule here is `high` risk. There is no such thing as a low-risk credential
sitting in a stored artifact.
"""

from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel

#: `(rule_id, compiled pattern, human message)`. Order is irrelevant: every rule
#: runs, and overlapping spans are merged by the redactor.
PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("secret.anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
     "Anthropic-style API key."),
    # After the Anthropic rule, and still matched independently: `sk-ant-…` also
    # satisfies this, and the redactor merges the overlapping spans.
    ("secret.openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
     "OpenAI-style API key."),
    ("secret.github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}"),
     "GitHub token."),
    ("secret.github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
     "GitHub fine-grained personal access token."),
    ("secret.aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
     "AWS access key id."),
    ("secret.private_key_block",
     re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
                r"[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"),
     "Private key block."),
    ("secret.jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "JWT-like token."),
    ("secret.slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
     "Slack token."),
)

#: `.env`-style assignment: a name that *says* it is a credential, and a value
#: long enough to be one. The value is captured so only it is redacted — blanking
#: the name would destroy the evidence that a credential was ever there.
ENV_ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:export[ \t]+)?"
    r"(?P<name>[A-Z][A-Z0-9_]*"
    r"(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIALS)"
    r"[A-Z0-9_]*)"
    r"[ \t]*=[ \t]*(?P<value>[\"']?[^\s\"']{8,}[\"']?)"
)

#: Placeholders an operator writes on purpose. Redacting these is noise, and noise
#: is what teaches people to ignore the scanner.
_PLACEHOLDER = re.compile(
    r"^[\"']?(?:x{3,}|\.{3,}|<[^>]+>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
    r"your[-_ ]?\w+|changeme|redacted|placeholder|example|dummy|fake|test)"
    r"[\"']?$",
    re.IGNORECASE,
)


def find(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule_id, pattern, message in PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(
                rule_id=rule_id, category=Category.secret, risk_level=RiskLevel.high,
                message=message, start=match.start(), end=match.end(),
            ))

    for match in ENV_ASSIGNMENT.finditer(text):
        value = match.group("value")
        if _PLACEHOLDER.match(value.strip()):
            continue
        findings.append(Finding(
            rule_id="secret.env_assignment", category=Category.secret,
            risk_level=RiskLevel.high,
            message=f"Secret-shaped assignment to {match.group('name')}.",
            start=match.start("value"), end=match.end("value"),
        ))
    return findings
