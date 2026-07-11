"""The Router is the product: it must build and run with the Model Manager package
absent (handoff Goal 2/3). We simulate absence by blocking the import."""

import builtins

import pytest

from agentconnect.router import mcp_server


def test_try_embedded_manager_returns_none_when_uninstalled(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        # Relative imports pass a truncated name (e.g. "model_manager.residency"),
        # so match on substring rather than a full dotted prefix.
        if "model_manager" in name:
            raise ImportError("simulated: agentconnect-model-manager not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delenv("MODEL_MANAGER_URL", raising=False)

    assert mcp_server._try_embedded_manager() is None


def test_build_service_standalone_cloud_only(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if "model_manager" in name:
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    monkeypatch.delenv("MODEL_MANAGER_URL", raising=False)
    monkeypatch.setenv("AGENTCONNECT_DB", str(tmp_path / "mem.sqlite"))

    svc = mcp_server._build_service()
    status = svc.get_router_status()
    assert status["local_manager"] is None  # no local node, but the router works
    assert "gemini_free" in status["providers"]


def test_public_task_routes_cloud_when_no_local(monkeypatch, tmp_path):
    from agentconnect.common.schemas import TaskConstraints, TaskState, TaskSubmission
    from agentconnect.router.service import RouterService

    svc = RouterService.create(memory=None, local_client=None)  # no local node at all
    sub = TaskSubmission(
        task="Classify: is 'the sky is blue' a question?",
        agent_type="log_summarizer",
        constraints=TaskConstraints(privacy_class="public"),
    )
    summary = svc.submit_task(sub)
    assert summary.status == TaskState.COMPLETE
    decisions = svc.memory.get_routing_decisions(summary.task_id)
    assert decisions[-1]["selected_provider"] in {"gemini_free", "groq_free", "openai_paid"}


def test_backplane_serves_with_no_sibling_connect_product_importable(monkeypatch, tmp_path):
    """First-family-release standalone proof: the backplane builds, serves HTTP, and
    degrades memory gracefully with NONE of BrainConnect (`wiki`/`brainconnect`),
    ComputeConnect, or ToolConnect importable.

    The same property was verified empirically against a wheels-only venv (none of
    the sibling repos installed) during the 0.1.0 clean-install run; this pins it in
    the gate. The configured-but-unreachable BrainConnect URL is the realistic
    degraded state: recall must warn, never raise.
    """
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        root = name.split(".")[0]
        if root in ("wiki", "brainconnect", "computeconnect", "toolconnect", "cognee",
                    "graphiti"):
            raise ImportError(f"simulated absence of sibling product {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    from fastapi.testclient import TestClient

    from agentconnect.api.app import create_app
    from agentconnect.core.memory import WikiBrainMemoryAdapter
    from agentconnect.core.models import CreateTaskRequest
    from agentconnect.core.service import AgentConnectService

    service = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "artifacts"),
        # Configured but pointing at nothing: the sibling is absent, not misconfigured.
        memory_backends={"brainconnect": WikiBrainMemoryAdapter(
            base_url="http://127.0.0.1:1", timeout=0.2, backend_name="brainconnect")},
    )
    task = service.create_task(CreateTaskRequest(title="standalone", created_by="test"))

    client = TestClient(create_app(service=service))
    assert client.get("/health").json()["status"] == "ok"

    # Memory is optional and never fatal: an unreachable authority degrades the
    # pack with a warning instead of failing the request.
    pack = service.get_task_context_pack(task.id, profile="manager_brief")
    assert any("recall failed" in w for w in pack.warnings)
    health = service.memory_health()
    assert health["backends"]["brainconnect"]["status"] == "unreachable"
