"""Gitleaks — an installed executable, not a Python dependency.

Gitleaks performs no verification lookups, so there is no network switch to hold
down; it is offline by construction. It is listed alongside TruffleHog because they
overlap: an operator picks one, or both, and AgentConnect unions whatever answers.
Neither is required.

`--report-path /dev/stdout` keeps the report in the pipe rather than on disk. A
report file would be a second copy of every secret Gitleaks found, written by the
tool that exists to stop secrets spreading. The report is read into memory, used to
locate the span, and discarded; the secret never reaches a `Finding`, a log line, or
artifact metadata.

`--redact` is deliberately **not** passed. Redacted output gives us the rule but no
locatable value, and a secret we cannot place is a secret we cannot mask — the
pipeline would then have to quarantine the whole artifact rather than clean it.

Exit code 1 means "leaks found", not "error". Treating a non-zero exit as failure
would fail closed on every successful detection — which is why `ExternalToolEngine`
does not check the return code and parses stdout instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import Capability, Finding, RiskLevel
from .base import ExternalToolEngine


class GitleaksEngine(ExternalToolEngine):
    name = "gitleaks"
    version = "external"
    capabilities = frozenset({Capability.secrets, Capability.repository_secrets})
    executable = "gitleaks"

    def argv(self, target: Path) -> list[str]:
        return [self.executable, "detect", "--no-git",
                "--report-format", "json", "--report-path", "/dev/stdout",
                "--source", str(target.parent if target.is_file() else target)]

    def parse(self, stdout: str, text: str) -> list[Finding]:
        # Gitleaks emits one JSON array, not JSON lines.
        try:
            records = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(records, list):
            return []

        findings: list[Finding] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            rule = record.get("RuleID") or record.get("Description") or "gitleaks"
            # `Secret` is the value; `Match` is the surrounding line fragment. Prefer
            # the value: masking the fragment would eat the assignment's left side.
            raw = record.get("Secret") or record.get("Match") or ""
            start, end = self.locate(text, raw)
            findings.append(self.finding(
                rule_id=f"gitleaks.{str(rule).strip().lower()}",
                capability=Capability.secrets,
                risk_level=RiskLevel.high,
                message=f"Gitleaks: {rule}.",
                start=start, end=end, confidence=0.9,
            ))
        return findings
