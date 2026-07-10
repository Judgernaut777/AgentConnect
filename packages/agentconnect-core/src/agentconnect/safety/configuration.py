"""Engine configuration.

Only the baseline is enabled by default. Everything else is opt-in, because a
default install must stay lightweight and because an engine that is enabled but not
installed warns on every scan — correct, and intolerable as a default.

An unknown engine name is a configuration **error**, not a warning. A typo in
`detect_secrests:` would otherwise silently disable the engine an operator believes
is protecting them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class EngineConfig:
    enabled: bool = False
    #: A required engine that is unavailable or broken fails the scan closed.
    #: An optional one degrades it: findings from the engines that did run still
    #: stand, and the result carries a warning and never reads as clean.
    required: bool = False
    #: Passed to the engine's constructor: `executable`, `timeout_seconds`,
    #: `model`, `use_entropy_plugins`, and so on.
    options: dict[str, Any] = field(default_factory=dict)


#: The default install: the standard-library baseline, and nothing else.
#: `required=True` because it is always available, so its absence would mean the
#: registry itself is broken.
DEFAULT_ENGINES: dict[str, EngineConfig] = {
    "baseline": EngineConfig(enabled=True, required=True),
}


@dataclass(frozen=True)
class SafetyConfig:
    enabled: bool = True
    engines: dict[str, EngineConfig] = field(
        default_factory=lambda: dict(DEFAULT_ENGINES))
    #: `{surface: [engine names]}`. Overrides the capability-based default
    #: selection when an operator wants a specific engine on a specific surface.
    surfaces: dict[str, list[str]] = field(default_factory=dict)

    def engine(self, name: str) -> EngineConfig:
        return self.engines.get(name, EngineConfig())

    def enabled_engines(self) -> list[str]:
        return [name for name, cfg in self.engines.items() if cfg.enabled]

    @classmethod
    def from_dict(cls, raw: Optional[dict[str, Any]]) -> "SafetyConfig":
        """Parse the `safety:` block. Unknown engine names raise."""
        from .registry import KNOWN_ENGINES

        section = (raw or {}).get("safety", raw) or {}
        engines = dict(DEFAULT_ENGINES)
        for name, spec in (section.get("engines") or {}).items():
            if name not in KNOWN_ENGINES:
                raise ValueError(
                    f"unknown safety engine {name!r}; known: {', '.join(sorted(KNOWN_ENGINES))}")
            spec = spec or {}
            options = {k: v for k, v in spec.items()
                       if k not in ("enabled", "required", "options")}
            options.update(spec.get("options") or {})
            engines[name] = EngineConfig(
                enabled=bool(spec.get("enabled", False)),
                required=bool(spec.get("required", name == "baseline")),
                options=options,
            )

        surfaces: dict[str, list[str]] = {}
        for surface, names in (section.get("surfaces") or {}).items():
            for name in names or []:
                if name not in KNOWN_ENGINES:
                    raise ValueError(
                        f"unknown safety engine {name!r} for surface {surface!r}")
            surfaces[surface] = list(names or [])

        return cls(enabled=bool(section.get("enabled", True)),
                   engines=engines, surfaces=surfaces)
