"""The authoritative catalog of MCP tools, and the action each one needs.

**This module is the single source of truth.** `agentconnect-mcp` registers exactly
these tools, `workspace.mcp_config` advertises exactly these names, and a test
asserts both. Before this existed there were two hand-written lists that had drifted:
`.mcp.json` advertised `get_subtask_status`, which no server registered, and omitted
eight tools that were registered — including every memory tool. A catalog nobody
generates is a catalog nobody keeps.

`core` cannot import `agentconnect-mcp` (the dependency runs the other way), so the
list lives here and the server imports it. That direction is deliberate: the ledger
decides what may be done to it, and a transport may only expose a subset.

Each tool names the action `AgentConnectService.authorize` is asked about. The action
is not the tool name — `get_status` the tool asks about `get_status` the action, but
`open_task` asks about `get_task` — because actions are ledger operations and several
tools may reach the same one.
"""

from __future__ import annotations

from typing import NamedTuple


class McpTool(NamedTuple):
    name: str
    action: str
    #: True when calling it changes the ledger. `get_handoff_summary` is a write:
    #: it persists the summary it computes.
    mutates: bool = False


#: Every tool `agentconnect-mcp` registers, in registration order.
MCP_TOOLS: tuple[McpTool, ...] = (
    McpTool("create_task", "create_task", mutates=True),
    McpTool("open_task", "get_task"),
    McpTool("get_handoff_summary", "get_handoff_summary", mutates=True),
    McpTool("claim_task", "claim_task", mutates=True),
    McpTool("release_task", "release_task", mutates=True),
    McpTool("record_decision", "record_decision", mutates=True),
    McpTool("record_attempt", "record_attempt", mutates=True),
    McpTool("request_review", "request_review", mutates=True),
    McpTool("submit_subtask", "submit_subtask", mutates=True),
    McpTool("get_status", "get_status"),
    McpTool("list_artifacts", "list_artifacts"),
    McpTool("read_artifact_chunk", "read_artifact_chunk"),
    McpTool("explain_route", "explain_route"),
    McpTool("recall_memory", "recall_memory"),
    McpTool("capture_memory_candidate", "capture_memory_candidate", mutates=True),
    McpTool("record_memory_feedback", "record_memory_feedback", mutates=True),
    McpTool("get_task_context_pack", "get_task_context_pack"),
)

MCP_TOOL_NAMES: tuple[str, ...] = tuple(t.name for t in MCP_TOOLS)
ACTION_FOR_TOOL: dict[str, str] = {t.name: t.action for t in MCP_TOOLS}

#: Never exposed, and written into `.mcp.json` so the denial is auditable rather
#: than implicit. None of these is registered by any server: the deny is structural,
#: and this list is the statement of intent, not the mechanism.
DENIED_MCP_TOOLS: tuple[str, ...] = (
    "temporal_signal", "wikibrain_promote", "brainconnect_promote",
    "cognee_write", "graphiti_write",
    "local_model_generate", "secrets_read",
)
