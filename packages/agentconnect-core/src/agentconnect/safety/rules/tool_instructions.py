"""Directives aimed at an agent's tools rather than at its reasoning.

Distinct from `prompt_injection` because the failure mode is different. Injection
changes what the agent *believes*; a tool directive changes what the agent
*does* — reads `~/.ssh`, prints its environment, pipes a download into a shell.

Two risk tiers, and the boundary is exfiltration:

* `high` — the text names a destination for credentials, or destroys data. Nothing
  legitimate in an artifact asks an agent to send secrets somewhere.
* `medium` — the text names a sensitive read. Common in documentation and in
  incident write-ups, so it warns and labels; it does not withhold.
"""

from __future__ import annotations

import re

from ..models import Category, Finding, RiskLevel

PATTERNS: tuple[tuple[str, re.Pattern[str], RiskLevel, str], ...] = (
    ("tool.exfiltrate_secrets",
     re.compile(r"(?i)\b(?:send|post|upload|exfiltrate|email|transmit)\s+"
                r"(?:the\s+|your\s+|all\s+)?(?:secrets?|credentials?|api[\s_-]?keys?|tokens?|"
                r"passwords?|env(?:ironment)?\s+(?:vars?|variables?))\s+to\b"),
     RiskLevel.high, "Instructs the agent to send credentials somewhere."),
    ("tool.curl_pipe_shell",
     re.compile(r"(?i)\bcurl\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b"),
     RiskLevel.high, "Pipes a download straight into a shell."),
    ("tool.destructive_rm",
     re.compile(r"(?i)\brm\s+(?:-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(?:/|~|\$HOME)(?:\s|$)"),
     RiskLevel.high, "Destructive recursive delete of a root or home path."),
    ("tool.read_ssh",
     re.compile(r"(?i)(?:\bread\b|\bcat\b|\bopen\b|\bcopy\b)?[^\n]{0,20}~?/?\.ssh(?:/|\b)"),
     RiskLevel.medium, "References the SSH key directory."),
    ("tool.print_environment",
     re.compile(r"(?i)\b(?:print|dump|show|list|output|echo)\s+"
                r"(?:the\s+|all\s+|your\s+)?env(?:ironment)?\s*(?:vars?|variables?)?\b"),
     RiskLevel.medium, "Asks the agent to print its environment."),
    ("tool.read_credentials_file",
     re.compile(r"(?i)(?:\.aws/credentials|\.netrc|\.git-credentials|"
                r"id_rsa\b|id_ed25519\b|\.env\.agentconnect\b)"),
     RiskLevel.medium, "References a credentials file."),
)


def find(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule_id, pattern, risk, message in PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(
                rule_id=rule_id, category=Category.tool_instruction, risk_level=risk,
                message=message, start=match.start(), end=match.end(),
            ))
    return findings
