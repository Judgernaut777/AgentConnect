"""A maintained prompt-injection classifier, loaded locally.

Optional and heavy: `pip install "agentconnect-core[safety-injection]"`.

Three constraints, each from a real failure mode:

* **No implicit download.** A managed agent run must not block on a model fetch, and
  a scanner must not make a network call while inspecting the content it is meant to
  contain. `local_files_only` defaults to true.
* **A pinned model.** `model` has no default. An unpinned classifier means a scan's
  verdict changes when a remote weight changes, and nothing in the ledger records it.
  The engine reports unavailable until an operator names the model.
* **Bounded input.** Text is truncated to the model's window; long artifacts are
  chunked and max-pooled rather than silently cut.

The classifier is never the authority. It produces one whole-text score with no span,
so it can warn or withhold but never redact. Detection of injection is evadable in
principle; policy, containment, and the fact that recalled memory is labeled
untrusted are what actually bound the damage.

Status: adapter implemented against the transformers sequence-classification API and
covered by fake-backed tests. **Not** exercised against real model weights in this
repository's gate.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models import Capability, Finding, RiskLevel
from .base import BaseEngine, EngineScanRequest

DEFAULT_THRESHOLD = 0.5
MAX_TOKENS = 512

#: Labels a binary classifier uses for the malicious class, lowercased.
MALICIOUS_LABELS = ("injection", "malicious", "jailbreak", "unsafe", "label_1")


class PromptGuardEngine(BaseEngine):
    name = "prompt_guard"
    version = "unknown"
    capabilities = frozenset({Capability.prompt_injection})

    def __init__(self, model: Optional[str] = None, threshold: float = DEFAULT_THRESHOLD,
                 local_files_only: bool = True, **_: Any) -> None:
        self.model_id = model
        self.threshold = float(threshold)
        self.local_files_only = bool(local_files_only)
        self._tokenizer: Any = None
        self._model: Any = None
        self.version = model or "unpinned"

    def available(self) -> bool:
        if not self.model_id:
            return False  # unpinned: a moving verdict is not a verdict
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _load(self) -> tuple[Any, Any]:
        if self._model is None:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            kwargs = {"local_files_only": self.local_files_only}
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_id, **kwargs)
            self._model = AutoModelForSequenceClassification.from_pretrained(
                self.model_id, **kwargs)
            self._model.eval()
        return self._tokenizer, self._model

    def _malicious_index(self, model: Any) -> int:
        id2label = getattr(model.config, "id2label", None) or {}
        for index, label in id2label.items():
            if str(label).strip().lower() in MALICIOUS_LABELS:
                return int(index)
        return -1  # binary models place the malicious class last

    def score(self, text: str) -> float:
        import torch

        tokenizer, model = self._load()
        encoded = tokenizer(text, return_tensors="pt", truncation=True,
                            max_length=MAX_TOKENS, stride=64,
                            return_overflowing_tokens=True)
        index = self._malicious_index(model)
        best = 0.0
        with torch.no_grad():
            for chunk in range(encoded["input_ids"].shape[0]):
                logits = model(encoded["input_ids"][chunk:chunk + 1],
                               attention_mask=encoded["attention_mask"][chunk:chunk + 1]).logits
                best = max(best, float(torch.softmax(logits, dim=-1)[0][index]))
        return best

    def scan(self, request: EngineScanRequest) -> list[Finding]:
        score = self.score(request.text)
        if score < self.threshold:
            return []
        risk = (RiskLevel.high if score >= 0.9
                else RiskLevel.medium if score >= 0.7 else RiskLevel.low)
        # No span: a whole-text score cannot be redacted, only warned about or withheld.
        return [self.finding(
            rule_id="prompt_guard.injection", capability=Capability.prompt_injection,
            risk_level=risk, message=f"Injection classifier score {score:.2f}.",
            confidence=score, metadata={"model": self.model_id},
        )]
