"""The `agentconnect-router` production entrypoint (mcp_server._build_service) must
wire the final-invocation-boundary tool governor from env/config — without this,
RouterService.tool_governor stays None on the real deployment path and the
ADR-0009 per-call authorize+redeem gate in the in-process act/tool loop is
unreachable dead capability (every side-effecting tool call runs ungoverned)."""

from __future__ import annotations

from agentconnect.core.toolconnect_client import ToolConnectGovernor
from agentconnect.router import mcp_server


def _clear_governor_env(monkeypatch):
    for k in (
        "AGENTCONNECT_TOOLCONNECT_URL",
        "AGENTCONNECT_TOOLCONNECT_TOKEN",
        "AGENTCONNECT_TOOLCONNECT_MODE",
        "AGENTCONNECT_TOOLCONNECT_TIMEOUT",
        "AGENTCONNECT_TOOLCONNECT_CONFIG",
    ):
        monkeypatch.delenv(k, raising=False)


def _isolate_service_env(monkeypatch, tmp_path):
    # Keep _build_service hermetic: tmp DB, no embedded manager (HttpLocalClient
    # does not connect at construction), default-deny spend authorizer.
    monkeypatch.setenv("AGENTCONNECT_DB", str(tmp_path / "mem.sqlite"))
    monkeypatch.setenv("MODEL_MANAGER_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("AGENTCONNECT_SPEND_AUTHORIZER", raising=False)


def test_build_service_wires_tool_governor_from_env(monkeypatch, tmp_path):
    _clear_governor_env(monkeypatch)
    _isolate_service_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_TOKEN", "tok")

    svc = mcp_server._build_service()
    assert isinstance(svc.tool_governor, ToolConnectGovernor)
    assert svc.tool_governor.base_url == "http://127.0.0.1:8095"
    assert svc.tool_governor.token == "tok"
    principal = svc.governed_principal
    assert principal is not None and principal["id"] == "router:agentconnect-router"
    assert principal["privacy_tier"] == "local"

    # And the governor genuinely reaches the enforcement seam: the built-in
    # in-process runtime built by _make_local_runtime carries it.
    class _NullSource:
        def generate(self, req):  # pragma: no cover — construction-only
            raise AssertionError("not called")

    from agentconnect.runtime import RuntimeConfig

    runtime = svc._make_local_runtime(_NullSource(), RuntimeConfig(workspace_root=str(tmp_path)))
    assert runtime._tool_governor is svc.tool_governor
    assert runtime._governed_principal == principal


def test_build_service_without_env_leaves_governor_unbound(monkeypatch, tmp_path):
    _clear_governor_env(monkeypatch)
    _isolate_service_env(monkeypatch, tmp_path)

    svc = mcp_server._build_service()
    assert svc.tool_governor is None
    assert svc.governed_principal is None
