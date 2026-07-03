"""Agent runtime for AgentConnect: the worker layer behind the router.

A LangGraph act/tool loop executes one task inside a confined workspace using
filesystem + shell tools, then returns the shared ``WorkerResult`` contract.
The model is reached through the ``ModelSource`` protocol, satisfied by the
model-manager backends and the router's local clients alike.
"""

from .actions import Action, parse_action
from .agent import AgentRuntime, LangGraphAgentRuntime, ModelSource, RuntimeConfig
from .results import worker_result_from_state
from .state import RuntimeState
from .workspace import Workspace, WorkspaceError

__all__ = [
    "Action",
    "AgentRuntime",
    "LangGraphAgentRuntime",
    "ModelSource",
    "RuntimeConfig",
    "RuntimeState",
    "Workspace",
    "WorkspaceError",
    "parse_action",
    "worker_result_from_state",
]
