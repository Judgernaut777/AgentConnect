"""One way to build the service from the environment (spec §5, §8).

Every adapter — MCP, HTTP, CLI — constructs its `AgentConnectService` here, so a
CLI command and an MCP tool run against the same database, the same artifact
directory, and the same worker registry. Point two adapters at the same
`AGENTCONNECT_DB_PATH` and they are looking at one ledger.

Env:
  AGENTCONNECT_DB_PATH        sqlite file (default ~/.agentconnect/agentconnect.db)
  AGENTCONNECT_ARTIFACT_DIR   artifact bodies (default ~/.agentconnect/artifacts)
  AGENTCONNECT_MAX_COST_USD   standing budget ceiling for routing (default 0.0)
  AGENTCONNECT_WORKERS        comma-separated built-ins to register (default "echo")
  AGENTCONNECT_WORKSPACE_DIR  managed agent workspaces (default ~/.agentconnect/workspaces)
  AGENTCONNECT_API_URL        what a launched agent is told to call (default :8790)

Memory backend env (each optional; a backend with no URL set stays off):
  WIKIBRAIN_URL / BRAINCONNECT_URL   the trusted-authority base URL
  WIKIBRAIN_TOKEN / BRAINCONNECT_TOKEN  bearer token for a token-protected server
                                     (`brainconnect serve --token`); sent as the
                                     Authorization header, never logged
  COGNEE_URL / COGNEE_TOKEN, GRAPHITI_URL / GRAPHITI_TOKEN   likewise
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .context import MemoryConfig
from .memory import (
    CogneeMemoryAdapter,
    GraphitiMemoryAdapter,
    MemoryAdapter,
    WikiBrainMemoryAdapter,
)
from .routing import RoutePolicy
from .service import AgentConnectService
from .workers import EchoWorker, WorkerAdapter

_log = logging.getLogger(__name__)

#: Backend name -> (adapter class, env var holding its base URL, default URL,
#: env var holding its bearer token). "brainconnect" is WikiBrain renamed: the
#: SAME adapter and the same trusted authority, registered under the new service
#: string (its packs and health then report the name it was configured under).
#: Configure ONE of the two — with both configured they are two clients of one
#: service, and the trusted-authority lookup resolves whichever the config names
#: (aliases match either way). The token env var mirrors the URL var: a
#: token-protected `brainconnect serve` (--token / BRAINCONNECT_TOKEN) is reached
#: by setting BRAINCONNECT_TOKEN here, and the wikibrain alias by WIKIBRAIN_TOKEN.
_MEMORY_BACKENDS: dict[str, tuple[type[MemoryAdapter], str, str, str]] = {
    "wikibrain": (WikiBrainMemoryAdapter, "WIKIBRAIN_URL", "http://localhost:8787", "WIKIBRAIN_TOKEN"),
    "brainconnect": (WikiBrainMemoryAdapter, "BRAINCONNECT_URL", "http://localhost:8787", "BRAINCONNECT_TOKEN"),
    "cognee": (CogneeMemoryAdapter, "COGNEE_URL", "http://localhost:8001", "COGNEE_TOKEN"),
    "graphiti": (GraphitiMemoryAdapter, "GRAPHITI_URL", "http://localhost:8002", "GRAPHITI_TOKEN"),
}

MEMORY_CONFIG_PATH = "AGENTCONNECT_MEMORY_CONFIG"
SAFETY_CONFIG_PATH = "AGENTCONNECT_SAFETY_CONFIG"

#: Built-in, dependency-free workers. Real harnesses (LiteLLM, local model
#: manager, Deep Agents, sandboxed shell) register themselves at runtime — the
#: core never imports them (§3: this is not a model gateway).
_BUILTIN_WORKERS = {"echo": EchoWorker}


def workers_from_env() -> list[WorkerAdapter]:
    names = os.environ.get("AGENTCONNECT_WORKERS", "echo")
    workers: list[WorkerAdapter] = []
    for raw in names.split(","):
        name = raw.strip()
        if not name:
            continue
        factory = _BUILTIN_WORKERS.get(name)
        if factory is None:
            _log.warning("unknown built-in worker %r in AGENTCONNECT_WORKERS; skipping", name)
            continue
        workers.append(factory())
    return workers


def policy_from_env() -> RoutePolicy:
    raw = os.environ.get("AGENTCONNECT_MAX_COST_USD", "0")
    try:
        return RoutePolicy(max_cost_usd=float(raw))
    except ValueError:
        _log.warning("AGENTCONNECT_MAX_COST_USD=%r is not a number; defaulting to 0", raw)
        return RoutePolicy()


def _load_memory_yaml() -> dict[str, Any]:
    path = Path(os.environ.get(MEMORY_CONFIG_PATH, "config/memory.yaml"))
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _log.warning("could not read %s (%s); memory stays disabled", path, exc)
        return {}


def safety_from_env() -> Optional["safety.SafetyPipeline"]:
    """Build the configured engine pipeline, or `None` for the default.

    `None` means the standard-library baseline and nothing else, which is what a
    default install should get: no heavy dependency, no subprocess, no model.

    A **malformed** safety config raises. Memory degrades to "off" when its YAML is
    unreadable, because a missing brain is a smaller problem than a wrong one. Safety
    is the opposite: an operator who wrote `detect_secrests:` believes an engine is
    running, and starting up quietly without it is the failure this whole layer
    exists to prevent.
    """
    from .. import safety

    path = Path(os.environ.get(SAFETY_CONFIG_PATH, "config/safety.yaml"))
    if not path.exists():
        return None
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = safety.SafetyConfig.from_dict(raw)  # unknown engine -> ValueError
    pipeline = safety.SafetyPipeline(config)
    for row in pipeline.status():
        if row["enabled"] and not row["available"]:
            _log.warning(
                "safety engine %s is enabled but unavailable (not installed, or its "
                "model or binary is missing)", row["engine"])
    return pipeline


def memory_from_env() -> tuple[dict[str, MemoryAdapter], MemoryConfig]:
    """Build whichever of WikiBrain / Cognee / Graphiti are configured.

    Every one of them is optional, and an absent config file means memory is
    simply off — the backplane is a task ledger first (§17 acceptance 1-2).
    """
    raw = _load_memory_yaml()
    config = MemoryConfig.from_dict(raw)
    if not config.enabled:
        return {}, config

    declared = (raw.get("memory") or {}).get("backends") or {}
    adapters: dict[str, MemoryAdapter] = {}
    for name, (cls, env_var, default_url, token_env) in _MEMORY_BACKENDS.items():
        spec = declared.get(name) or {}
        if declared and not spec.get("enabled", False):
            continue
        if not declared and not os.environ.get(env_var):
            continue  # nothing configured for this backend at all
        base_url = os.environ.get(env_var) or spec.get("base_url") or default_url
        # Optional bearer token, mirroring the base-URL precedence: env wins over
        # the memory.yaml `token`, and neither means an unauthenticated client
        # (api_key stays None). Never logged — a token in a warning line is a leak.
        api_key = os.environ.get(token_env) or spec.get("token") or None
        if cls is WikiBrainMemoryAdapter:
            # Registered under the name it was configured as ("wikibrain" or
            # "brainconnect"), so packs and health report what the operator wrote.
            adapters[name] = cls(base_url=base_url, backend_name=name, api_key=api_key)
        else:
            adapters[name] = cls(base_url=base_url, api_key=api_key)  # type: ignore[call-arg]

    if not adapters:
        _log.info("no memory backends configured; context packs will be task state only")
    return adapters, config


DEFAULT_API_URL = "http://localhost:8790"


def service_from_env(
    workers: Optional[list[WorkerAdapter]] = None,
    db_path: Optional[str] = None,
    artifact_dir: Optional[str] = None,
    workspace_dir: Optional[str] = None,
) -> AgentConnectService:
    adapters, memory_config = memory_from_env()
    return AgentConnectService.create(
        db_path=db_path or os.environ.get("AGENTCONNECT_DB_PATH"),
        artifact_dir=artifact_dir or os.environ.get("AGENTCONNECT_ARTIFACT_DIR"),
        workers=workers if workers is not None else workers_from_env(),
        policy=policy_from_env(),
        memory_backends=adapters,
        memory_config=memory_config,
        workspace_dir=workspace_dir or os.environ.get("AGENTCONNECT_WORKSPACE_DIR"),
        api_url=os.environ.get("AGENTCONNECT_API_URL", DEFAULT_API_URL),
        safety_pipeline=safety_from_env(),
    )
