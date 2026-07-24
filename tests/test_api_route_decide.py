"""`/route/decide` degrades to 503 without a router — and without the package.

The README promises that a deployment with no router package/config answers this
one route with a 503. A module-top `from agentconnect.router.routing import ...`
made that promise unkeepable for the package-absent case: `pip install
agentconnect-core agentconnect-api` crashed at import, before `deps.router_from_env`'s
guard could ever run. These regressions pin the lazy import and the 503 itself.
"""

from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient

from agentconnect.api import routes_route
from agentconnect.api.app import create_app
from agentconnect.core.service import AgentConnectService


class _BlockRouterPackage:
    """A meta-path finder that makes `agentconnect.router*` unimportable, standing in
    for a core+api-only install where agentconnect-router simply is not there."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agentconnect.router" or fullname.startswith("agentconnect.router."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


def test_routes_route_imports_without_the_router_package(monkeypatch):
    # Evict any cached router modules, then forbid re-importing them.
    for name in [m for m in list(sys.modules)
                 if m == "agentconnect.router" or m.startswith("agentconnect.router.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BlockRouterPackage()] + sys.meta_path)

    # Re-executing the module must not raise ModuleNotFoundError: the router
    # import lives inside the handler, behind the 503 guard.
    importlib.reload(routes_route)


def test_route_decide_returns_503_when_no_router_is_configured(tmp_path):
    svc = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "artifacts"))
    token = svc.mint_operator_token("operator")
    # `router=None` is the caller-supplied "this deployment has no router" case.
    client = TestClient(create_app(service=svc, linear_sync=None, router=None))
    client.headers.update({"Authorization": f"Bearer {token.plaintext}"})

    response = client.post("/route/decide", json={
        "task_id": "task_x", "privacy_class": "public"})

    assert response.status_code == 503
    assert "router is not configured" in response.json()["detail"]
