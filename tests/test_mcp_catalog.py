"""The generated MCP catalog matches the tools that actually exist.

`.mcp.json` used to advertise `get_subtask_status`, which no server registered, and
omit eight tools that were — every memory tool among them. A harness honoring
`allowedTools` therefore denied a manager its memory and granted it a tool it could
not call. Two hand-written lists, drifting quietly.

There is now one list, `core.tools.MCP_TOOLS`, and these tests hold everything else
to it.
"""

from __future__ import annotations

import asyncio

import pytest

from agentconnect.core.service import AgentConnectService
from agentconnect.core.tools import (
    ACTION_FOR_TOOL,
    DENIED_MCP_TOOLS,
    MCP_TOOL_NAMES,
    MCP_TOOLS,
)
from agentconnect.core.sessions import (
    MANAGER_ACTIONS,
    NEVER_TOKEN_ACTIONS,
    OPERATOR_ACTIONS,
)
from agentconnect.core.workspace import DENIED_MCP_TOOLS as WS_DENIED
from agentconnect.core.workspace import EXPOSED_MCP_TOOLS, mcp_config


@pytest.fixture()
def svc(tmp_path):
    return AgentConnectService.create(
        db_path=str(tmp_path / "l.db"),
        artifact_dir=str(tmp_path / "a"),
        workspace_dir=str(tmp_path / "w"),
    )


def registered_tools(svc) -> set[str]:
    from agentconnect.mcp.server import build_mcp_server

    server = build_mcp_server(svc)
    tools = asyncio.new_event_loop().run_until_complete(server.list_tools())
    return {t.name for t in tools}


def test_the_server_registers_exactly_the_catalog(svc):
    assert registered_tools(svc) == set(MCP_TOOL_NAMES)


def test_the_workspace_catalog_is_generated_from_the_same_source(svc):
    assert set(EXPOSED_MCP_TOOLS) == set(MCP_TOOL_NAMES)
    assert WS_DENIED == DENIED_MCP_TOOLS


def test_every_advertised_tool_resolves_to_a_real_tool(svc):
    """The `get_subtask_status` bug, made unrepeatable."""
    config = mcp_config("http://localhost:8130", {"AGENTCONNECT_TASK_ID": "task_1"})
    advertised = {name.rsplit("__", 1)[-1] for name in config["allowedTools"]}
    assert advertised == registered_tools(svc)
    assert "get_subtask_status" not in advertised


def test_no_forbidden_tool_is_registered(svc):
    assert not (registered_tools(svc) & set(DENIED_MCP_TOOLS))


def test_the_denied_tools_are_written_into_the_config_for_audit():
    config = mcp_config("http://localhost:8130", {})
    assert config["deniedTools"] == list(DENIED_MCP_TOOLS)


def test_every_tool_names_an_action_a_manager_actually_holds():
    """A tool a manager cannot authorize is a tool a manager cannot call."""
    for tool in MCP_TOOLS:
        assert tool.action in MANAGER_ACTIONS, f"{tool.name} -> {tool.action}"


def test_no_tool_maps_to_an_action_no_token_may_reach():
    assert not (set(ACTION_FOR_TOOL.values()) & NEVER_TOKEN_ACTIONS)


def test_completion_and_promotion_are_reachable_by_no_tool():
    """The structural deny: there is no tool to call, whatever a scope claims."""
    for action in ("complete_task", "force_complete_task", "promote_memory_candidate"):
        assert action not in ACTION_FOR_TOOL.values()
        assert action in OPERATOR_ACTIONS  # an operator can, out of band


# ------------------------------------------------- the token gates MCP calls

def test_an_mcp_tool_call_is_authorized_against_the_session_token(svc, monkeypatch, tmp_path):
    """Previously `authorize()` was called by no adapter at all."""
    from agentconnect.core.models import CreateTaskRequest

    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="operator"))
    launched = svc.launch_session("claude", task_id=task.id, claim=True)
    monkeypatch.setenv("AGENTCONNECT_SESSION_TOKEN", launched["token"])
    monkeypatch.setenv("AGENTCONNECT_TASK_ID", task.id)

    from agentconnect.mcp.server import build_mcp_server

    server = build_mcp_server(svc)
    loop = asyncio.new_event_loop()

    allowed = loop.run_until_complete(
        server.call_tool("record_attempt", {"task_id": task.id, "actor_id": "claude",
                                            "summary": "worked"}))
    assert "forbidden" not in str(allowed).lower()

    # A revoked token stops every tool, mid-session.
    svc.end_shell(launched["session"].id, 0)
    refused = loop.run_until_complete(
        server.call_tool("record_attempt", {"task_id": task.id, "actor_id": "claude",
                                            "summary": "after the shell exited"}))
    assert "revoked" in str(refused).lower()


def test_an_mcp_tool_cannot_reach_another_task(svc, monkeypatch):
    from agentconnect.core.models import CreateTaskRequest

    mine = svc.create_task(CreateTaskRequest(title="mine", goal="g", created_by="op"))
    yours = svc.create_task(CreateTaskRequest(title="yours", goal="g", created_by="op"))
    launched = svc.launch_session("claude", task_id=mine.id, claim=True)
    monkeypatch.setenv("AGENTCONNECT_SESSION_TOKEN", launched["token"])

    from agentconnect.mcp.server import build_mcp_server

    server = build_mcp_server(svc)
    loop = asyncio.new_event_loop()
    refused = loop.run_until_complete(
        server.call_tool("record_attempt", {"task_id": yours.id, "actor_id": "claude",
                                            "summary": "not my task"}))
    assert "scoped to task_id" in str(refused)


def test_without_a_session_token_the_mcp_server_is_an_operator_tool(svc, monkeypatch):
    """An operator running the server by hand holds no session. Enforcement is theirs."""
    from agentconnect.core.models import CreateTaskRequest

    monkeypatch.delenv("AGENTCONNECT_SESSION_TOKEN", raising=False)
    task = svc.create_task(CreateTaskRequest(title="t", goal="g", created_by="op"))

    from agentconnect.mcp.server import build_mcp_server

    server = build_mcp_server(svc)
    result = asyncio.new_event_loop().run_until_complete(
        server.call_tool("open_task", {"task_id": task.id}))
    assert task.id in str(result)
