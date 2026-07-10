"""GLiNER — entity-based PII, configured independently of Presidio.

Optional and heavy: model weights are downloaded on first use, which is exactly
what must not happen in the middle of a managed agent run. The adapter therefore
never downloads implicitly — `local_files_only` defaults to true, and an absent
model reports `available() is False` rather than reaching for the network.

Use it for entity classes simple rules and Presidio's recognizers handle poorly:
people, organizations, deployment-specific sensitive nouns. Configure the labels;
there is no universal set.

Status: adapter implemented against GLiNER's documented `predict_entities` API and
covered by fake-backed tests. **Not** exercised against installed GLiNER weights in
this repository's gate.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

DEFAULT_LABELS = ("person", "organization", "address", "phone number", "email")
DEFAULT_THRESHOLD = 0.5


class GlinerEngine(BaseEngine):
    name = "gliner"
    version = "unknown"
    capabilities = frozenset({Capability.pii})

    def __init__(self, model: Optional[str] = None,
                 labels: Optional[list[str]] = None,
                 threshold: float = DEFAULT_THRESHOLD,
                 local_files_only: bool = True, **_: Any) -> None:
        self.model_id = model
        self.labels = list(labels) if labels else list(DEFAULT_LABELS)
        self.threshold = float(threshold)
        self.local_files_only = bool(local_files_only)
        self._model: Any = None

    def available(self) -> bool:
        if not self.model_id:
            return False  # no model pinned: nothing to load, and we will not guess
        try:
            import gliner  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _load(self) -> Any:
        if self._model is None:
            from gliner import GLiNER

            self._model = GLiNER.from_pretrained(
                self.model_id, local_files_only=self.local_files_only)
        return self._model

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        entities = self._load().predict_entities(
            request.text, self.labels, threshold=self.threshold)
        findings: list[Finding] = []
        for entity in entities:
            label = str(entity.get("label", "entity"))
            score = float(entity.get("score", 0.0))
            findings.append(self.finding(
                rule_id=f"gliner.{label.replace(' ', '_').lower()}",
                capability=Capability.pii, risk_level=RiskLevel.medium,
                message=f"GLiNER: {label}.",
                start=int(entity.get("start", 0)), end=int(entity.get("end", 0)),
                confidence=score,
            ))
        return findings
