"""Observability configuration surface (production handoff Part IV).

One env-driven builder, so every adapter (CLI, HTTP, MCP) constructs the same
provider set against the same policy. Defaults are the whole point:

* `AGENTCONNECT_OBSERVABILITY` unset  =>  no providers  =>  effectively noop.
  A standalone AgentConnect install requires **no** provider (Part IV).
* Any named provider that cannot be built (bad tmux, Herdr with no socket) is
  handled by the failure policy: `startup_fatal` aborts; otherwise it is dropped
  with a warning and the rest still run.

Env surface:
  AGENTCONNECT_OBSERVABILITY              comma list: structured_log,tmux,herdr,otlp
  AGENTCONNECT_OBSERVABILITY_FAILURE_POLICY   advisory | task_blocking | startup_fatal
  AGENTCONNECT_OBSERVABILITY_LOG_PATH     JSONL path (default ~/.agentconnect/observability/events.jsonl)
  AGENTCONNECT_OBSERVABILITY_TMUX_SOCKET  dedicated tmux socket (default agentconnect-obs)
  AGENTCONNECT_OBSERVABILITY_TMUX_LAYOUT  tmux layout hint (default tiled)
  AGENTCONNECT_OBSERVABILITY_HERDR_ENABLED   1/true to enable the Herdr provider
  AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET    Herdr control socket path
  AGENTCONNECT_OTLP_ENDPOINT              OTLP collector base URL (enables otlp export)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .providers.composite import CompositeObservabilityProvider, FailurePolicy
from .providers.herdr import HerdrObservabilityProvider
from .providers.noop import NoopObservabilityProvider
from .providers.otlp import OtlpExporterObservabilityProvider
from .providers.structured_log import StructuredLogObservabilityProvider
from .providers.tmux import TmuxObservabilityProvider
from .provider import AgentObservabilityProvider

_log = logging.getLogger(__name__)


def _default_log_path() -> str:
    return str(Path.home() / ".agentconnect" / "observability" / "events.jsonl")


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ObservabilityConfig:
    providers: list[str] = field(default_factory=list)
    failure_policy: FailurePolicy = FailurePolicy.advisory
    log_path: str = field(default_factory=_default_log_path)
    tmux_socket: str = "agentconnect-obs"
    tmux_layout: str = "tiled"
    herdr_enabled: bool = False
    herdr_socket: str = ""
    otlp_endpoint: str = ""

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "ObservabilityConfig":
        env = environ if environ is not None else os.environ
        raw = env.get("AGENTCONNECT_OBSERVABILITY", "")
        providers = [p.strip() for p in raw.split(",") if p.strip()]
        policy_raw = (env.get("AGENTCONNECT_OBSERVABILITY_FAILURE_POLICY") or "advisory").strip()
        try:
            policy = FailurePolicy(policy_raw)
        except ValueError:
            _log.warning("unknown observability failure policy %r; using advisory", policy_raw)
            policy = FailurePolicy.advisory
        otlp = (env.get("AGENTCONNECT_OTLP_ENDPOINT") or "").strip()
        # OTLP is implied whenever an endpoint is set, even if not named in the list.
        if otlp and "otlp" not in providers:
            providers.append("otlp")
        return cls(
            providers=providers,
            failure_policy=policy,
            log_path=env.get("AGENTCONNECT_OBSERVABILITY_LOG_PATH") or _default_log_path(),
            tmux_socket=env.get("AGENTCONNECT_OBSERVABILITY_TMUX_SOCKET") or "agentconnect-obs",
            tmux_layout=env.get("AGENTCONNECT_OBSERVABILITY_TMUX_LAYOUT") or "tiled",
            herdr_enabled=_truthy(env.get("AGENTCONNECT_OBSERVABILITY_HERDR_ENABLED")),
            herdr_socket=env.get("AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET") or "",
            otlp_endpoint=otlp,
        )

    def build_provider(self, redactor=None) -> CompositeObservabilityProvider:
        """Instantiate the configured providers into one composite.

        A provider that fails to construct is fatal under `startup_fatal` and
        dropped-with-warning otherwise, so a broken tmux never takes down a
        deployment that also logs to JSONL.
        """
        built: list[AgentObservabilityProvider] = []
        for name in self.providers:
            try:
                built.append(self._build_one(name, redactor))
            except Exception as exc:  # noqa: BLE001
                if self.failure_policy is FailurePolicy.startup_fatal:
                    raise
                _log.warning("observability provider %r failed to start: %s", name, exc)
        if not built:
            built.append(NoopObservabilityProvider())
        composite = CompositeObservabilityProvider(built, policy=self.failure_policy)
        if self.failure_policy is FailurePolicy.startup_fatal:
            for p in built:
                health = p.health()
                if not health.available:
                    raise RuntimeError(
                        f"observability provider {p.name!r} unhealthy at startup "
                        f"(startup_fatal): {health.detail}"
                    )
        return composite

    def _build_one(self, name: str, redactor) -> AgentObservabilityProvider:
        if name in ("noop", "none"):
            return NoopObservabilityProvider()
        if name in ("structured_log", "jsonl", "log"):
            return StructuredLogObservabilityProvider(self.log_path)
        if name == "tmux":
            return TmuxObservabilityProvider(socket=self.tmux_socket, redactor=redactor)
        if name == "herdr":
            return HerdrObservabilityProvider(
                enabled=self.herdr_enabled, socket_path=self.herdr_socket or None,
            )
        if name == "otlp":
            return OtlpExporterObservabilityProvider(endpoint=self.otlp_endpoint or None)
        raise ValueError(f"unknown observability provider {name!r}")
