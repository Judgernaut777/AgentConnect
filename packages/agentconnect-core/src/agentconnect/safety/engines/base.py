"""The engine contract.

AgentConnect owns *when* scanning happens, *which* engines run at each surface,
how their findings combine, and what the answer means for a task. An engine owns
detection and nothing else. It cannot decide whether an artifact persists, whether
a context item is delivered, or whether an audit passes — it returns findings, and
policy does the rest.

Two states that look alike and are not:

* **unavailable** — the library is not installed, the binary is not on `PATH`, the
  model is absent. The engine never read the content. `available()` says so
  *without* scanning, and must never raise.
* **failed** — the engine is present and it raised. It read the content, or tried
  to, and we do not know what it saw. This is the state that must never be
  rendered as `allow`.

An engine that cannot run reports `available() is False`. It does not raise. An
engine that breaks mid-scan raises, and the pipeline fails closed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from ..models import Capability, Finding

#: External tools are given a wall-clock budget. A scanner that hangs is a scanner
#: that stops the loop, and an operator will disable it rather than wait.
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass
class EngineScanRequest:
    """What an engine is asked to look at.

    `path` is set only for surfaces backed by a real file or directory. Engines
    with `repository_secrets` need one; text-only engines ignore it.
    """

    text: str
    surface: str
    path: Optional[Path] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SafetyEngine(Protocol):
    """Detection only. Never policy."""

    name: str
    version: str
    capabilities: frozenset[Capability]

    def available(self) -> bool:
        """Can this engine run right now? Must not scan, and must not raise."""

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        """Normalized findings. Raising means *failure*, not absence."""


class BaseEngine:
    """Shared plumbing. Subclasses set `name`, `version`, `capabilities`."""

    name: str = "engine"
    version: str = "0"
    capabilities: frozenset[Capability] = frozenset()

    def available(self) -> bool:  # pragma: no cover - overridden
        return True

    def scan(self, request: EngineScanRequest) -> list[Finding]:  # pragma: no cover
        raise NotImplementedError

    def finding(
        self, rule_id: str, capability: Capability, risk_level, message: str,
        start: int = 0, end: int = 0, confidence: float = 1.0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Finding:
        from ..models import CATEGORY_OF_CAPABILITY

        return Finding(
            rule_id=rule_id, category=CATEGORY_OF_CAPABILITY[capability],
            risk_level=risk_level, message=message, start=start, end=end,
            engine=self.name, engine_version=self.version, confidence=confidence,
            metadata=metadata or {},
        )


class ExternalToolEngine(BaseEngine):
    """Base for engines backed by an installed executable.

    Three rules, and each of them is load-bearing:

    1. **No network.** These tools offer to *verify* a candidate credential by
       calling the service it belongs to. Verification is exfiltration: it takes
       the secret we are trying to contain and sends it to a third party. It stays
       off unless an operator explicitly opts in, per invocation flags.
    2. **A timeout.** An unbounded subprocess is an unbounded `create_artifact`.
    3. **Structured output.** We parse JSON, never prose. A tool that changes its
       human-readable format should break loudly in tests, not silently return no
       findings in production.

    Raw matched values are used only to locate a span in the text, and are then
    dropped. They never reach a `Finding`, a log line, or artifact metadata.
    """

    executable: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allow_network_verification: bool = False

    def __init__(self, executable: Optional[str] = None,
                 timeout_seconds: Optional[float] = None,
                 allow_network_verification: bool = False, **_: Any) -> None:
        self.executable = executable or self.executable
        self.timeout_seconds = (
            self.timeout_seconds if timeout_seconds is None else float(timeout_seconds))
        self.allow_network_verification = bool(allow_network_verification)

    def available(self) -> bool:
        return bool(self.executable) and shutil.which(self.executable) is not None

    def argv(self, target: Path) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def parse(self, stdout: str, text: str) -> list[Finding]:  # pragma: no cover
        raise NotImplementedError

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        target = request.path
        with tempfile.TemporaryDirectory(prefix="agentconnect-safety-") as tmp:
            if target is None:
                target = Path(tmp) / "content.txt"
                target.write_text(request.text, encoding="utf-8")
            try:
                proc = subprocess.run(
                    self.argv(target), capture_output=True, text=True,
                    timeout=self.timeout_seconds, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"{self.name} exceeded {self.timeout_seconds}s") from exc
            except OSError as exc:  # binary vanished between available() and here
                raise RuntimeError(f"{self.name} could not run: {exc}") from exc
        return self.parse(proc.stdout, request.text)

    @staticmethod
    def json_lines(stdout: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def locate(text: str, raw: str) -> tuple[int, int]:
        """Span of `raw` in `text`, or `(0, 0)` when it cannot be placed.

        `raw` is the tool's copy of the secret. It is used here and discarded; a
        finding that cannot be placed is still reported, it just cannot be redacted
        by span — which is why an unplaceable secret escalates the whole scan.
        """
        if not raw:
            return (0, 0)
        index = text.find(raw)
        return (index, index + len(raw)) if index >= 0 else (0, 0)
