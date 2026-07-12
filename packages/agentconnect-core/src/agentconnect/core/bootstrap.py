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

Compute plane env (optional; unset => the local_model_manager worker stays off):
  AGENTCONNECT_COMPUTE_URL     base URL of a ComputeConnect deployment (env wins over
                               `config/compute.yaml` -> compute.base_url)
  AGENTCONNECT_COMPUTE_TIMEOUT request timeout seconds (default 30)
  AGENTCONNECT_COMPUTE_TOKEN   optional bearer token, Authorization header, never logged

Tool governance env (optional; unset => no ToolGovernor, standalone unchanged):
  AGENTCONNECT_TOOLCONNECT_URL   base URL of a `toolconnect serve` decision point
                                 (env wins over `config/toolconnect.yaml`)
  AGENTCONNECT_TOOLCONNECT_TOKEN optional bearer token, Authorization header, never logged
  AGENTCONNECT_TOOLCONNECT_MODE  `required` (default) or `advisory`
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from .context import MemoryConfig
from .local_compute import HttpLocalComputeProvider, LocalModelManagerWorkerAdapter
from .memory import (
    CogneeMemoryAdapter,
    GraphitiMemoryAdapter,
    MemoryAdapter,
    WikiBrainMemoryAdapter,
)
from .observability import ObservabilityConfig, ObservabilityEmitter
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
COMPUTE_CONFIG_PATH = "AGENTCONNECT_COMPUTE_CONFIG"
TOOLCONNECT_CONFIG_PATH = "AGENTCONNECT_TOOLCONNECT_CONFIG"

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


def _load_yaml_config(env_var: str, default_path: str, subsystem: str) -> dict[str, Any]:
    """Read a `config/*.yaml` with the memory-backend discipline: absent file means
    the feature is off, and a **malformed** file degrades to off with a warning — a
    missing subsystem is a smaller problem than a wrong one."""
    path = Path(os.environ.get(env_var, default_path))
    if not path.exists():
        return {}
    try:
        import yaml

        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _log.warning("could not read %s (%s); %s stays disabled", path, exc, subsystem)
        return {}


def _compute_timeout(block: dict[str, Any]) -> float:
    raw = os.environ.get("AGENTCONNECT_COMPUTE_TIMEOUT")
    if raw is None:
        raw = block.get("timeout")
    if raw is None:
        return 30.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        _log.warning("compute timeout %r is not a number; defaulting to 30", raw)
        return 30.0


def compute_worker_from_env() -> Optional[WorkerAdapter]:
    """Build the external local-compute worker from config, or `None` (subsystem off).

    Mirrors `memory_from_env` byte-for-byte on precedence and failure: env
    `AGENTCONNECT_COMPUTE_URL` wins over `config/compute.yaml`'s `compute.base_url`,
    file wins over nothing, and absence leaves the `local_model_manager` worker
    unregistered — exactly today's "optional subsystem" standalone behaviour. A
    malformed `compute:` block degrades to off with a warning. The result, when not
    `None`, is a `LocalModelManagerWorkerAdapter(HttpLocalComputeProvider(url))` —
    both already exist; only the wiring from config was missing (ComputeConnect
    docs/AGENTCONNECT_INTEGRATION.md). No engine choice is made here.
    """
    block = (_load_yaml_config(COMPUTE_CONFIG_PATH, "config/compute.yaml", "compute").get("compute")
             or {})
    env_url = os.environ.get("AGENTCONNECT_COMPUTE_URL")
    if env_url:
        url = env_url
    elif block.get("enabled") is False:
        return None  # explicitly disabled in yaml; env (checked above) can still force it on
    else:
        url = block.get("base_url")
    if not url:
        return None  # nothing configured — the subsystem stays off, as today

    token = os.environ.get("AGENTCONNECT_COMPUTE_TOKEN") or block.get("token") or None
    provider = HttpLocalComputeProvider(str(url), timeout=_compute_timeout(block), token=token)
    kwargs: dict[str, Any] = {}
    if block.get("worker_id"):
        kwargs["worker_id"] = str(block["worker_id"])
    if block.get("task_type"):
        kwargs["task_type"] = str(block["task_type"])
    if block.get("max_output_tokens") is not None:
        try:
            kwargs["max_output_tokens"] = int(block["max_output_tokens"])
        except (TypeError, ValueError):
            _log.warning(
                "compute max_output_tokens %r is not an int; using adapter default",
                block["max_output_tokens"])
    return LocalModelManagerWorkerAdapter(provider, **kwargs)


def toolconnect_governor_from_env() -> Optional[Any]:
    """Build the optional ToolConnect governor from config, or `None` (unchanged).

    Same env-over-file precedence and degrade-to-off-on-malformed discipline as memory:
    env `AGENTCONNECT_TOOLCONNECT_URL` wins over `config/toolconnect.yaml`'s
    `toolconnect.base_url`, and absence means no governor — standalone AgentConnect runs
    exactly as before. The governor itself is fail-*closed* (an unreachable engine denies),
    which is the one place AgentConnect deliberately departs from "adapters fail open"
    (ToolConnect docs/AGENTCONNECT_CONTRACT.md §1).
    """
    from .toolconnect_client import ToolConnectGovernor

    block = (_load_yaml_config(TOOLCONNECT_CONFIG_PATH, "config/toolconnect.yaml",
                               "toolconnect governor").get("toolconnect") or {})
    env_url = os.environ.get("AGENTCONNECT_TOOLCONNECT_URL")
    if env_url:
        url = env_url
    elif block.get("enabled") is False:
        return None
    else:
        url = block.get("base_url")
    if not url:
        return None

    token = os.environ.get("AGENTCONNECT_TOOLCONNECT_TOKEN") or block.get("token") or None
    mode = os.environ.get("AGENTCONNECT_TOOLCONNECT_MODE") or block.get("mode") or "required"
    raw_timeout = os.environ.get("AGENTCONNECT_TOOLCONNECT_TIMEOUT") or block.get("timeout") or 10.0
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        _log.warning("toolconnect timeout %r is not a number; defaulting to 10", raw_timeout)
        timeout = 10.0
    return ToolConnectGovernor(str(url), token=token, mode=str(mode), timeout=timeout)


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


def observability_from_env(service: AgentConnectService) -> ObservabilityEmitter:
    """Build the configured observability emitter (default: effectively noop).

    A standalone install sets nothing and gets a noop emitter — no provider is
    required. The emitter's redactor is the service's own safety-backed one, so
    `agents output` is bounded and redacted through AgentConnect's safety layer.
    """
    config = ObservabilityConfig.from_env()
    composite = config.build_provider(redactor=service.observation_redactor())
    return ObservabilityEmitter(composite, redactor=service.observation_redactor())


def service_from_env(
    workers: Optional[list[WorkerAdapter]] = None,
    db_path: Optional[str] = None,
    artifact_dir: Optional[str] = None,
    workspace_dir: Optional[str] = None,
) -> AgentConnectService:
    adapters, memory_config = memory_from_env()
    worker_list = list(workers if workers is not None else workers_from_env())
    # Append the external local-compute worker when configured (env or compute.yaml);
    # absent config leaves the list untouched, so standalone behaviour is unchanged.
    compute_worker = compute_worker_from_env()
    if compute_worker is not None:
        worker_list.append(compute_worker)
    service = AgentConnectService.create(
        db_path=db_path or os.environ.get("AGENTCONNECT_DB_PATH"),
        artifact_dir=artifact_dir or os.environ.get("AGENTCONNECT_ARTIFACT_DIR"),
        workers=worker_list,
        policy=policy_from_env(),
        memory_backends=adapters,
        memory_config=memory_config,
        workspace_dir=workspace_dir or os.environ.get("AGENTCONNECT_WORKSPACE_DIR"),
        api_url=os.environ.get("AGENTCONNECT_API_URL", DEFAULT_API_URL),
        safety_pipeline=safety_from_env(),
    )
    service.bind_observability(observability_from_env(service))
    # Optional, fail-closed tool governance. None when unconfigured — standalone unchanged.
    governor = toolconnect_governor_from_env()
    if governor is not None:
        service.bind_tool_governor(governor)
    return service
