"""Which engines exist, and how to build them.

Engine modules are imported lazily. Importing `agentconnect.safety` must not import
`torch`, and a registry that eagerly constructed every engine would make an
unavailable engine cost as much as an installed one.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .configuration import SafetyConfig
from .engines.base import SafetyEngine
from .models import Capability

_log = logging.getLogger(__name__)


def _baseline() -> type:
    from .engines.baseline import BaselineEngine

    return BaselineEngine


def _detect_secrets() -> type:
    from .engines.detect_secrets import DetectSecretsEngine

    return DetectSecretsEngine


def _trufflehog() -> type:
    from .engines.trufflehog import TruffleHogEngine

    return TruffleHogEngine


def _gitleaks() -> type:
    from .engines.gitleaks import GitleaksEngine

    return GitleaksEngine


def _presidio() -> type:
    from .engines.presidio import PresidioEngine

    return PresidioEngine


def _gliner() -> type:
    from .engines.gliner import GlinerEngine

    return GlinerEngine


def _prompt_guard() -> type:
    from .engines.prompt_guard import PromptGuardEngine

    return PromptGuardEngine


#: Every engine AgentConnect knows how to build. A name outside this map is a
#: configuration error, never a silently-ignored line of YAML.
KNOWN_ENGINES: dict[str, Callable[[], type]] = {
    "baseline": _baseline,
    "detect_secrets": _detect_secrets,
    "trufflehog": _trufflehog,
    "gitleaks": _gitleaks,
    "presidio": _presidio,
    "gliner": _gliner,
    "prompt_guard": _prompt_guard,
}


def build_engine(name: str, options: Optional[dict[str, Any]] = None) -> SafetyEngine:
    try:
        loader = KNOWN_ENGINES[name]
    except KeyError:
        raise ValueError(
            f"unknown safety engine {name!r}; known: {', '.join(sorted(KNOWN_ENGINES))}"
        ) from None
    return loader()(**(options or {}))


class EngineRegistry:
    """The engines this deployment has configured, built once."""

    def __init__(self, config: Optional[SafetyConfig] = None) -> None:
        self.config = config or SafetyConfig()
        self._engines: dict[str, SafetyEngine] = {}
        for name in self.config.enabled_engines():
            spec = self.config.engine(name)
            try:
                self._engines[name] = build_engine(name, spec.options)
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001 — a broken constructor is not a crash
                # Construction failing is not the same as the engine being absent:
                # an engine that cannot even be built is reported unavailable and
                # will fail closed if it was marked required.
                _log.warning("safety engine %s could not be constructed: %s", name, exc)

    def __contains__(self, name: str) -> bool:
        return name in self._engines

    def get(self, name: str) -> Optional[SafetyEngine]:
        return self._engines.get(name)

    def names(self) -> list[str]:
        return sorted(self._engines)

    def with_capability(self, capability: Capability) -> list[SafetyEngine]:
        return [e for name, e in sorted(self._engines.items())
                if capability in e.capabilities]

    def required(self, name: str) -> bool:
        return self.config.engine(name).required

    def status(self) -> list[dict[str, Any]]:
        """Health, for an operator: what is configured, and what can actually run.

        This is where "enabled but not installed" becomes visible without waiting
        for a scan to warn about it.
        """
        rows: list[dict[str, Any]] = []
        for name in sorted(set(self.config.engines) | set(self._engines)):
            spec = self.config.engine(name)
            engine = self._engines.get(name)
            available = False
            if engine is not None:
                try:
                    available = bool(engine.available())
                except Exception as exc:  # noqa: BLE001 — available() must not raise
                    _log.warning("safety engine %s available() raised: %s", name, exc)
            rows.append({
                "engine": name,
                "enabled": spec.enabled,
                "required": spec.required,
                "constructed": engine is not None,
                "available": available,
                "version": getattr(engine, "version", "") if engine else "",
                "capabilities": sorted(c.value for c in engine.capabilities) if engine else [],
            })
        return rows
