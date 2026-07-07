"""AgentConnect Temporal fork: an OPTIONAL durable-execution substrate.

The zero-infra SQLite ``WorkQueue`` remains the default. This package is for
deployments that already run a Temporal server: it gives durable execution, native
retries/timeouts/heartbeats, and DAG (child workflows) for free, while the
DIFFERENTIATED privacy×tier authorization stays ours — reused verbatim from
``agentconnect.common.privacy.admits``. Nothing here is imported by the default path.
"""

from .substrate import (
    AgentTaskParams,
    AgentTaskWorkflow,
    TemporalSubstrate,
    build_worker,
    start_agent_task,
)

__all__ = [
    "AgentTaskParams",
    "AgentTaskWorkflow",
    "TemporalSubstrate",
    "build_worker",
    "start_agent_task",
]
