"""Yelp `detect-secrets` — the first maintained secret engine.

Optional: `pip install "agentconnect-core[safety-secrets]"`. Absent, this reports
`available() is False` and the baseline carries on alone.

Two things this adapter gets right that are easy to get wrong:

**`scan_line` needs a settings context.** Outside `default_settings()`, plugin
discovery yields nothing and `scan_line` returns an empty iterator — no error, no
warning, just silence. An adapter without the context looks like it works, passes a
smoke test against a file with no secrets, and detects nothing forever. We assert
against a known key in the tests for exactly this reason.

**The entropy plugins are noisy on prose.** `Base64HighEntropyString` fires on
ordinary identifiers — scanning `aws_key = AKIA...` flags both the key *and* the
string `aws_key`. Redacting on that would eat the variable name and teach operators
to switch the engine off. They are therefore disabled by default and enabled with
`use_entropy_plugins: true`, which is worth doing on artifact bodies (config files,
`.env` dumps) and rarely worth doing on recalled prose.
"""

from __future__ import annotations

from typing import Any

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

#: Plugin type names whose findings are entropy heuristics rather than patterns.
ENTROPY_PLUGINS = frozenset({
    "Base64HighEntropyString", "HexHighEntropyString",
    "Base64 High Entropy String", "Hex High Entropy String",
})


def _slug(value: str) -> str:
    return str(value).strip().lower().replace(" ", "_")


#: Characters a credential is made of. Not "anything but whitespace": expanding
#: across `=` and `"` would swallow `key="…"` and redact the variable name, which is
#: the one part of a `.env` line worth keeping.
_CREDENTIAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-+/.~")


def expand_to_token(line: str, start: int, end: int) -> tuple[int, int]:
    """Widen a match to the credential token containing it.

    detect-secrets does not always return the whole credential. Its GitHub detector
    reports `secret_value == "ghp"` — the *prefix*. Redacting that span masks three
    characters and leaves the token sitting in the text, which is worse than not
    redacting at all: the marker announces the secret was handled while it is still
    there.

    Trailing `=` is base64 padding and belongs to the value; a leading `=` is an
    assignment operator and does not.
    """
    while start > 0 and line[start - 1] in _CREDENTIAL_CHARS:
        start -= 1
    while end < len(line) and line[end] in _CREDENTIAL_CHARS:
        end += 1
    while end < len(line) and line[end] == "=":
        end += 1
    return start, end


class DetectSecretsEngine(BaseEngine):
    name = "detect_secrets"
    version = "unknown"
    capabilities = frozenset({Capability.secrets})

    def __init__(self, use_entropy_plugins: bool = False, **_: Any) -> None:
        self.use_entropy_plugins = bool(use_entropy_plugins)
        self._resolve_version()

    def _resolve_version(self) -> None:
        try:
            from importlib.metadata import version

            self.version = version("detect-secrets")
        except Exception:  # noqa: BLE001 — version is metadata, never a scan blocker
            self.version = "unknown"

    def available(self) -> bool:
        try:
            import detect_secrets.core.scan  # noqa: F401
            from detect_secrets.settings import default_settings  # noqa: F401
        except Exception:  # noqa: BLE001 — absence is not failure
            return False
        return True

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        from detect_secrets.core.scan import scan_line
        from detect_secrets.settings import default_settings

        findings: list[Finding] = []
        with default_settings():
            offset = 0
            for line in request.text.splitlines(keepends=True):
                for secret in scan_line(line):
                    finding = self._normalize(secret, line, offset)
                    if finding is not None:
                        findings.append(finding)
                offset += len(line)
        return findings

    def _normalize(self, secret: Any, line: str, offset: int):
        kind = str(getattr(secret, "type", "") or "secret")
        if kind in ENTROPY_PLUGINS and not self.use_entropy_plugins:
            return None

        value = getattr(secret, "secret_value", "") or ""
        index = line.find(value) if value else -1
        if index >= 0:
            start, end = expand_to_token(line, index, index + len(value))
            start += offset
            end += offset
        else:
            start = end = 0

        # Entropy hits are heuristics; pattern hits name a credential format.
        entropy = kind in ENTROPY_PLUGINS
        return self.finding(
            rule_id=f"detect_secrets.{_slug(kind)}",
            capability=Capability.secrets,
            risk_level=RiskLevel.medium if entropy else RiskLevel.high,
            message=f"detect-secrets: {kind}.",
            start=start, end=end,
            confidence=0.6 if entropy else 0.9,
        )
