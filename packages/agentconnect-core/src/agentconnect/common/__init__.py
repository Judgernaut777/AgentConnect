"""Shared core for the Agent Router and Local Model Manager.

This package holds the pieces that are policy- and framework-agnostic: schemas,
config loaders, the task state machine, shared memory/artifact store, quota
ledger, privacy/redaction, provider registry, secret resolution, and token
estimation. It has no dependency on FastAPI or the MCP SDK, so it can be unit
tested in isolation (handoff §26: keep responsibilities separate).
"""

from __future__ import annotations

import importlib

__all__ = [
    "config",
    "memory",
    "privacy",
    "providers",
    "quota",
    "schemas",
    "secrets",
    "state",
    "tokens",
    "transitions",
]


def __getattr__(name: str):
    """Lazy submodule access (PEP 562): `agentconnect.common.config` (etc.)
    still works exactly as `from . import config, ...` used to, but nothing
    under this package is imported as a SIDE EFFECT of merely reaching
    `agentconnect.common.<some other submodule>`.

    This matters beyond tidiness: `agentconnect.core.service` gained its
    first-ever import of anything under `agentconnect.common` (the shared
    transition authority, `common.transitions` — pure stdlib, no filesystem
    work at import time) in the one-transition-authority consolidation. The
    OLD eager `from . import config, memory, ...` here meant that single new
    import edge also eagerly ran `config.py`'s module-level
    `_discover_config_dir()` (`Path.cwd()`, `Path(__file__).resolve()`) as a
    side effect — filesystem syscalls Temporal's workflow sandbox restricts,
    which broke `Worker.__init__`'s `prepare_workflow` validation for every
    workflow that (transitively, via `agentconnect.core.service`) reaches
    `agentconnect.temporal.activities`. Nothing in this codebase relies on
    `import agentconnect.common` alone pre-loading every submodule as an
    attribute (every real caller imports the specific submodule it needs), so
    this is a behavior-preserving fix, not a compatibility break.
    """
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
