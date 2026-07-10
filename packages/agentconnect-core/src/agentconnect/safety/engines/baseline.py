"""The always-available engine: standard library, deterministic, offline.

This is a **lightweight floor**, not enterprise-grade detection. It exists so that:

* a default `pip install agentconnect-core` is never a no-op,
* the gate runs fast and offline,
* an operator with no engines configured still gets the obvious cases,
* and there is always a fallback when a maintained engine is unavailable.

It is *not* a substitute for detect-secrets, TruffleHog, Gitleaks, Presidio, or a
maintained injection classifier. Do not grow it into one. New rules belong here
only when they serve a safe default, offline operation, or regression coverage.

Its PII coverage is deliberately nil: partial PII detection is worse than none,
because it reads as coverage. Enable Presidio.
"""

from __future__ import annotations

from dataclasses import replace

from ..baseline import encoding, prompt_injection, secrets, tool_instructions
from ..models import Capability, Finding
from .base import BaseEngine, EngineScanRequest

#: `(capability, module)`. Each module exposes `find(text) -> list[Finding]`.
RULESETS = (
    (Capability.secrets, secrets),
    (Capability.prompt_injection, prompt_injection),
    (Capability.tool_control, tool_instructions),
    (Capability.encoded_content, encoding),
)


class BaselineEngine(BaseEngine):
    name = "baseline"
    version = "1"
    capabilities = frozenset({
        Capability.secrets, Capability.prompt_injection,
        Capability.tool_control, Capability.encoded_content,
    })

    def __init__(self, **_: object) -> None:
        pass

    def available(self) -> bool:
        return True

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        """A rule that raises propagates. The pipeline decides what that means —
        this engine does not get to declare its own failure harmless."""
        findings: list[Finding] = []
        for _capability, module in RULESETS:
            for finding in module.find(request.text):
                findings.append(replace(finding, engine=self.name,
                                        engine_version=self.version))
        return findings
