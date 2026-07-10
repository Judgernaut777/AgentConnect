"""TruffleHog — an installed executable, not a Python dependency.

`--no-verification` is the flag that matters. TruffleHog can confirm a candidate
credential by authenticating against the service it belongs to. For a scanner whose
job is to stop credentials escaping, that is exfiltration performed by the guard
itself. It stays off unless an operator sets `allow_network_verification`, and the
default install never sends a byte anywhere.

`--results=verified,unknown,unverified` follows from that: with verification off,
every hit is "unverified", and TruffleHog hides those by default. Without this flag
the adapter runs, exits zero, prints nothing, and reports a clean scan forever.

Suited to artifacts, files, and repositories. Not to short context snippets: a
subprocess per recalled memory item is a poor trade, so no surface runs it on
`context_output` by default.
"""

from __future__ import annotations

from pathlib import Path

from ..models import Capability, Finding, RiskLevel
from .base import ExternalToolEngine


class TruffleHogEngine(ExternalToolEngine):
    name = "trufflehog"
    version = "external"
    capabilities = frozenset({Capability.secrets, Capability.repository_secrets})
    executable = "trufflehog"

    def argv(self, target: Path) -> list[str]:
        argv = [self.executable, "filesystem", str(target),
                "--json", "--no-update",
                "--results=verified,unknown,unverified"]
        if not self.allow_network_verification:
            argv.append("--no-verification")
        return argv

    def parse(self, stdout: str, text: str) -> list[Finding]:
        findings: list[Finding] = []
        for record in self.json_lines(stdout):
            detector = record.get("DetectorName") or record.get("DetectorType") or "unknown"
            raw = record.get("Raw") or record.get("RawV2") or ""
            start, end = self.locate(text, raw)
            verified = bool(record.get("Verified"))
            findings.append(self.finding(
                rule_id=f"trufflehog.{str(detector).strip().lower()}",
                capability=Capability.secrets,
                risk_level=RiskLevel.high,
                message=f"TruffleHog: {detector} credential.",
                start=start, end=end,
                confidence=1.0 if verified else 0.9,
                metadata={"verified": verified},
            ))
        return findings
