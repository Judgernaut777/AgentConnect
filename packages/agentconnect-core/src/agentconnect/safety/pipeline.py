"""surface → policy → engines → normalized findings → enforcement.

The pipeline is the only thing that turns findings into a decision. Engines cannot
do it: they do not know which surface they ran on, and two of them are subprocesses.

**Aggregation is a union, never an intersection.** If the baseline finds nothing and
detect-secrets finds a GitHub token, there is a GitHub token. An engine that stays
silent has abstained, not vetoed. What deduplication removes is *two descriptions of
the same span*, and even then both engines keep their attribution — two engines
agreeing is evidence, and one engine's false positive is a bug report against that
engine rather than against the pipeline.

**Failure is not absence, and neither is clean.** Five engine states, and no two of
them collapse:

| state | meaning | effect |
|---|---|---|
| `ok` | it read the content | its findings count |
| `ok`, no findings | it read the content and found nothing | contributes nothing |
| `skipped` | disabled, or lacks a capability this surface wants | nothing, silently |
| `unavailable` | never installed; it never looked | required → fail closed; optional → warn |
| `failed` / `timeout` | present, and it raised | required → fail closed; optional → never `allow` |
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

from .configuration import SafetyConfig
from .engines.base import EngineScanRequest, SafetyEngine
from .models import (
    POLICY_VERSION,
    Category,
    Decision,
    EngineOutcome,
    EngineStatus,
    Finding,
    RiskLevel,
    SafetyBatchResult,
    SafetyItem,
    SafetyResult,
    highest,
    strongest,
)
from .policies import Policy, policy as _policy
from .redaction import redact
from .registry import EngineRegistry

_log = logging.getLogger(__name__)


def _overlaps(a: Finding, b: Finding) -> bool:
    if not (a.has_span and b.has_span):
        return False
    return a.start < b.end and b.start < a.end


def aggregate(findings: list[Finding]) -> list[Finding]:
    """Union, then collapse duplicate descriptions of one span.

    Two engines flagging the same credential must produce **one** redaction. Emitting
    two would nest markers inside each other and corrupt the offsets of everything
    after. The survivor is the highest-risk, highest-confidence finding; the losers
    are recorded in `metadata["also_detected_by"]` so attribution is never lost.
    """
    ordered = sorted(
        findings,
        key=lambda f: (f.category.value, f.start, -(f.end - f.start),
                       -_RISK_RANK[f.risk_level], -f.confidence),
    )
    merged: list[Finding] = []
    for finding in ordered:
        for index, kept in enumerate(merged):
            if kept.category is finding.category and _overlaps(kept, finding):
                merged[index] = _absorb(kept, finding)
                break
        else:
            merged.append(finding)
    return merged


_RISK_RANK = {RiskLevel.none: 0, RiskLevel.low: 1, RiskLevel.medium: 2, RiskLevel.high: 3}


def _absorb(kept: Finding, other: Finding) -> Finding:
    """`kept` wins the span; `other` keeps its attribution in the metadata."""
    from dataclasses import replace

    attribution = list(kept.metadata.get("also_detected_by", []))
    entry = {"engine": other.engine, "rule_id": other.rule_id,
             "confidence": other.confidence}
    if entry not in attribution and other.engine != kept.engine:
        attribution.append(entry)

    # The strongest risk over the merged span survives, and so does the widest span:
    # one engine may see `sk-ant-…` where another sees only `sk-…`.
    return replace(
        kept,
        risk_level=kept.risk_level if _RISK_RANK[kept.risk_level] >= _RISK_RANK[other.risk_level]
        else other.risk_level,
        start=min(kept.start, other.start),
        end=max(kept.end, other.end),
        confidence=max(kept.confidence, other.confidence),
        metadata={**kept.metadata,
                  **({"also_detected_by": attribution} if attribution else {})},
    )


class SafetyPipeline:
    """Built once per service. Engines are constructed here, not per scan."""

    def __init__(self, config: Optional[SafetyConfig] = None,
                 registry: Optional[EngineRegistry] = None) -> None:
        self.config = config or SafetyConfig()
        self.registry = registry or EngineRegistry(self.config)

    # ------------------------------------------------------------- selection
    def engines_for(self, surface: str) -> list[tuple[str, SafetyEngine]]:
        """Policy decides. An engine never selects itself onto a surface.

        An explicit `surfaces:` override wins; otherwise an engine runs where its
        capabilities intersect what the surface asked for.
        """
        pol = _policy(surface)
        override = self.config.surfaces.get(surface)
        if override is not None:
            return [(name, engine) for name in override
                    if (engine := self.registry.get(name)) is not None]

        selected: list[tuple[str, SafetyEngine]] = []
        for name in self.registry.names():
            engine = self.registry.get(name)
            assert engine is not None
            if engine.capabilities & pol.capabilities:
                selected.append((name, engine))
        return selected

    def status(self) -> list[dict]:
        return self.registry.status()

    # ------------------------------------------------------------------ scan
    def scan_text(self, content: str, *, surface: str, policy: Optional[str] = None,
                  path: Optional[Path] = None) -> SafetyResult:
        pol = _policy(policy or surface)
        text = content or ""
        request = EngineScanRequest(text=text, surface=surface, path=path)

        outcomes = [self._run(name, engine, request)
                    for name, engine in self.engines_for(pol.name)]
        findings = aggregate([f for o in outcomes for f in o.findings])

        decision, risk, warnings, failed = self._decide(findings, outcomes, pol)
        body = self._redact(text, findings, pol)

        if not findings and not warnings:
            return SafetyResult(decision=Decision.allow, risk_level=RiskLevel.none,
                                redacted_content=text, policy_version=POLICY_VERSION,
                                engines=outcomes)

        return SafetyResult(
            decision=decision, risk_level=risk, findings=findings, redacted_content=body,
            labels=_labels(findings), warnings=warnings, policy_version=POLICY_VERSION,
            scanner_failed=failed, engines=outcomes,
        )

    def scan_items(self, items: Iterable[SafetyItem], *, policy: str) -> SafetyBatchResult:
        batch = SafetyBatchResult(policy_version=POLICY_VERSION)
        for item in items:
            batch.results[item.id] = self.scan_text(item.text, surface=policy)
        return batch

    # -------------------------------------------------------------- internals
    def _run(self, name: str, engine: SafetyEngine, request: EngineScanRequest) -> EngineOutcome:
        required = self.registry.required(name)
        version = getattr(engine, "version", "")
        try:
            if not engine.available():
                return EngineOutcome(name, EngineStatus.unavailable, required, version=version)
        except Exception as exc:  # noqa: BLE001 — available() is contractually silent
            _log.warning("safety engine %s available() raised: %s", name, exc)
            return EngineOutcome(name, EngineStatus.failed, required,
                                 error=f"available() raised: {exc}", version=version)

        try:
            findings = list(engine.scan(request))
        except TimeoutError as exc:
            _log.warning("safety engine %s timed out: %s", name, exc)
            return EngineOutcome(name, EngineStatus.timeout, required,
                                 error=str(exc), version=version)
        except Exception as exc:  # noqa: BLE001 — a broken engine must not read as clean
            _log.warning("safety engine %s failed: %s", name, exc)
            return EngineOutcome(name, EngineStatus.failed, required,
                                 error=str(exc), version=version)
        return EngineOutcome(name, EngineStatus.ok, required, findings=findings,
                             version=version)

    def _decide(self, findings: list[Finding], outcomes: list[EngineOutcome],
                pol: Policy) -> tuple[Decision, RiskLevel, list[str], bool]:
        decisions: list[Decision] = []
        warnings: list[str] = []
        failed = False

        for finding in findings:
            if finding.category is Category.scanner_error:
                continue
            decision = pol.rules.get((finding.category, finding.risk_level), Decision.allow)
            if decision is Decision.redact and not finding.has_span:
                # An engine found a credential and could not tell us *where*. There is
                # nothing to mask, so masking would be a lie: the marker would say the
                # secret was handled while the secret stayed in the text. Withhold the
                # whole thing instead.
                decision = Decision.quarantine
                warnings.append(
                    f"safety engine {finding.engine!r} reported {finding.rule_id} "
                    f"without a location; content cannot be redacted and was withheld.")
            decisions.append(decision)

        for outcome in outcomes:
            if outcome.status is EngineStatus.ok:
                continue
            if outcome.status is EngineStatus.skipped:
                continue

            if outcome.broke:
                failed = True
                warnings.append(
                    f"safety engine {outcome.name!r} "
                    f"{'timed out' if outcome.status is EngineStatus.timeout else 'failed'}"
                    f"{f': {outcome.error}' if outcome.error else ''}; "
                    f"content was not fully scanned.")
                # A required engine that broke fails the surface closed. An optional
                # one cannot, on its own, withhold content the other engines read —
                # but it can never leave the result at `allow`.
                decisions.append(pol.on_scanner_error if outcome.required
                                 else pol.on_optional_engine_error)
            elif outcome.status is EngineStatus.unavailable:
                warnings.append(
                    f"safety engine {outcome.name!r} is enabled but unavailable "
                    f"(not installed, or its model or binary is missing).")
                if outcome.required:
                    failed = True
                    decisions.append(pol.on_scanner_error)

        return strongest(decisions), highest([f.risk_level for f in findings]), warnings, failed

    def _redact(self, text: str, findings: list[Finding], pol: Policy) -> str:
        """Redact only what the policy decided to redact, and only where a span
        exists. A whole-text classifier score has none: it can withhold the item,
        never rewrite it."""
        to_redact = [
            f for f in findings
            if f.has_span
            and f.category is not Category.scanner_error
            and pol.rules.get((f.category, f.risk_level)) is Decision.redact
        ]
        return redact(text, to_redact) if to_redact else text


def _labels(findings: list[Finding]) -> list[str]:
    seen: dict[str, None] = {}
    for finding in findings:
        seen.setdefault(f"safety:{finding.category.value}", None)
    for finding in findings:
        seen.setdefault(f"engine:{finding.engine}", None)
    return list(seen)
