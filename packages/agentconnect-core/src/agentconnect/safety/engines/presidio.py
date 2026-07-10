"""Microsoft Presidio — the preferred structured PII engine.

Optional: `pip install "agentconnect-core[safety-pii]"` plus a spaCy model
(`python -m spacy download en_core_web_lg`). Absent, `available()` is False.

**AgentConnect has no PII rules of its own, and that is deliberate.** A homegrown
regex for names and addresses is worse than nothing: it catches the easy third,
misses the rest, and the coverage it appears to provide is the reason nobody
installs a real engine. Presidio is the answer to PII here; the baseline abstains.

Presidio runs entirely in-process. It loads a local NLP model and makes no remote
call. Its analyzer is expensive to construct, so it is built once, lazily, on first
scan rather than at registry time — an unavailable engine must cost nothing.

Status: adapter implemented against Presidio's documented `AnalyzerEngine.analyze`
API and covered by fake-backed tests. It has **not** been exercised against an
installed Presidio in this repository's gate; the library is not present here.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

#: Presidio scores in [0, 1]. Below this, entity recall is dominated by noise.
DEFAULT_THRESHOLD = 0.5

#: Entity types worth a stronger risk level than the default.
_HIGH_RISK_ENTITIES = frozenset({
    "US_SSN", "CREDIT_CARD", "IBAN_CODE", "US_BANK_NUMBER", "US_PASSPORT",
    "MEDICAL_LICENSE", "CRYPTO",
})


class PresidioEngine(BaseEngine):
    name = "presidio"
    version = "unknown"
    capabilities = frozenset({Capability.pii})

    def __init__(self, language: str = "en", threshold: float = DEFAULT_THRESHOLD,
                 entities: Optional[list[str]] = None, **_: Any) -> None:
        self.language = language
        self.threshold = float(threshold)
        self.entities = list(entities) if entities else None
        self._analyzer: Any = None
        self._resolve_version()

    def _resolve_version(self) -> None:
        try:
            from importlib.metadata import version

            self.version = version("presidio-analyzer")
        except Exception:  # noqa: BLE001
            self.version = "unknown"

    def available(self) -> bool:
        try:
            import presidio_analyzer  # noqa: F401
        except Exception:  # noqa: BLE001 — absence is not failure
            return False
        return True

    def _engine(self) -> Any:
        if self._analyzer is None:
            from presidio_analyzer import AnalyzerEngine

            self._analyzer = AnalyzerEngine()
        return self._analyzer

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        results = self._engine().analyze(
            text=request.text, language=self.language, entities=self.entities)
        findings: list[Finding] = []
        for result in results:
            score = float(getattr(result, "score", 0.0))
            if score < self.threshold:
                continue
            entity = str(getattr(result, "entity_type", "PII"))
            findings.append(self.finding(
                rule_id=f"presidio.{entity.lower()}",
                capability=Capability.pii,
                risk_level=(RiskLevel.high if entity in _HIGH_RISK_ENTITIES
                            else RiskLevel.medium),
                message=f"Presidio: {entity}.",
                start=int(result.start), end=int(result.end), confidence=score,
            ))
        return findings
