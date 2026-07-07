"""Multi-harness transport selection for agentconnect-router.

The router is one MCP server that any harness can drive. Model A runs a stdio router
per harness over a shared AGENTCONNECT_DB; Model B runs ONE SSE/streamable-HTTP router
that many harnesses connect to. These tests cover the env-driven transport resolution
and host/port threading (not the blocking server run itself).
"""

import pytest

from agentconnect.common.memory import SharedMemory
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.mcp_server import _transport_from_env, build_mcp_server
from agentconnect.router.service import RouterService


def _clear_env(monkeypatch):
    for k in ("AGENTCONNECT_MCP_TRANSPORT", "AGENTCONNECT_MCP_HOST", "AGENTCONNECT_MCP_PORT"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_to_stdio(monkeypatch):
    _clear_env(monkeypatch)
    assert _transport_from_env() == ("stdio", "127.0.0.1", 8760)


def test_env_selects_shared_http_instance(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENTCONNECT_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("AGENTCONNECT_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENTCONNECT_MCP_PORT", "9001")
    assert _transport_from_env() == ("streamable-http", "0.0.0.0", 9001)


def test_sse_transport_accepted(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENTCONNECT_MCP_TRANSPORT", "SSE")  # case/space tolerant
    assert _transport_from_env()[0] == "sse"


def test_invalid_transport_fails_closed(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AGENTCONNECT_MCP_TRANSPORT", "websocket")
    with pytest.raises(SystemExit) as exc:
        _transport_from_env()
    assert "AGENTCONNECT_MCP_TRANSPORT" in str(exc.value)


def test_build_threads_host_and_port_into_server():
    svc = RouterService.create(
        memory=SharedMemory(), local_client=InProcessLocalClient(ResidencyManager())
    )
    mcp = build_mcp_server(service=svc, host="0.0.0.0", port=9002)
    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 9002


def test_stdio_build_leaves_default_bind():
    svc = RouterService.create(
        memory=SharedMemory(), local_client=InProcessLocalClient(ResidencyManager())
    )
    mcp = build_mcp_server(service=svc)  # no host/port -> FastMCP defaults, unchanged
    assert mcp.settings.port == 8000
