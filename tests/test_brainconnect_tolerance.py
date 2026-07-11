"""BrainConnect rename tolerance: "wikibrain" and "brainconnect" are one authority.

BrainConnect is WikiBrain renamed (module `wiki` → `brainconnect`, service string
"wikibrain" → "brainconnect"), and the rename lands in the sibling repo on its own
schedule. This suite pins AgentConnect's side of the transition: every place a
backend *name* is matched accepts either spelling, while trust semantics stay
exactly where they were — on the role and the authority's verdict, never a string.
"""
from __future__ import annotations

from agentconnect.core.bootstrap import memory_from_env
from agentconnect.core.context import MemoryConfig, MemoryRouter
from agentconnect.core.memory import (
    TRUSTED_AUTHORITY_NAMES,
    MemoryItem,
    StaticMemoryAdapter,
    WikiBrainMemoryAdapter,
    backend_aliases,
    resolve_backend,
)
from agentconnect.core.service import AgentConnectService
from agentconnect.core.sessions import AGENT_FORBIDDEN_ACTIONS, NEVER_TOKEN_ACTIONS
from agentconnect.core.tools import DENIED_MCP_TOOLS


def _adapter(name: str) -> WikiBrainMemoryAdapter:
    return WikiBrainMemoryAdapter(
        transport=lambda m, u, p: {"items": []}, backend_name=name)


# ------------------------------------------------------------------ aliases
def test_authority_names_alias_each_other_and_nothing_else():
    assert backend_aliases("wikibrain") == TRUSTED_AUTHORITY_NAMES
    assert backend_aliases("brainconnect") == TRUSTED_AUTHORITY_NAMES
    assert backend_aliases("cognee") == frozenset({"cognee"})


def test_resolve_backend_matches_across_the_rename_both_ways():
    brain = _adapter("brainconnect")
    wiki = _adapter("wikibrain")
    assert resolve_backend({"brainconnect": brain}, "wikibrain") is brain
    assert resolve_backend({"wikibrain": wiki}, "brainconnect") is wiki
    assert resolve_backend({}, "wikibrain") is None


def test_resolve_backend_prefers_the_exact_name():
    brain, wiki = _adapter("brainconnect"), _adapter("wikibrain")
    both = {"brainconnect": brain, "wikibrain": wiki}
    assert resolve_backend(both, "brainconnect") is brain
    assert resolve_backend(both, "wikibrain") is wiki


# ------------------------------------------------------------------ service
def test_service_resolves_brainconnect_as_the_trusted_authority(tmp_path):
    """Default config says `trusted_authority: wikibrain`; the deployment registered
    the renamed service. The authority must still resolve — silently losing the
    trusted authority would silently drop the promotion path."""
    brain = _adapter("brainconnect")
    service = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "artifacts"),
        memory_backends={"brainconnect": brain},
    )
    assert service.memory_config.trusted_authority == "wikibrain"  # unchanged default
    assert service.trusted_authority() is brain
    assert service.memory is brain  # the bare recall path answers from the authority


def test_service_resolves_wikibrain_when_config_names_brainconnect(tmp_path):
    wiki = _adapter("wikibrain")
    config = MemoryConfig(trusted_authority="brainconnect")
    service = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "artifacts"),
        memory_backends={"wikibrain": wiki}, memory_config=config,
    )
    assert service.trusted_authority() is wiki


def test_aliasing_confers_no_trust_on_a_retrieval_engine(tmp_path):
    """A backend that merely *names itself* brainconnect is not the authority:
    `trusted_authority()` requires a TrustedMemoryAdapter, exactly as before."""
    impostor = StaticMemoryAdapter(
        items=[MemoryItem(text="x", status="promoted", confidence="high")],
        backend_name="brainconnect",
    )
    service = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "artifacts"),
        memory_backends={"brainconnect": impostor},
    )
    assert service.trusted_authority() is None


# ------------------------------------------------------------------ profiles
def test_profiles_written_as_wikibrain_select_a_brainconnect_adapter():
    router = MemoryRouter(MemoryConfig(), {
        "brainconnect": _adapter("brainconnect"),
        "cognee": StaticMemoryAdapter(backend_name="cognee"),
    })
    assert router.select_backends("manager_brief") == ["brainconnect", "cognee"]
    # implementation_constraints is authority-only, and must not fall back to
    # querying every adapter just because the authority changed its name.
    assert router.select_backends("implementation_constraints") == ["brainconnect"]


def test_exact_profile_name_still_wins_and_never_doubles():
    adapters = {
        "wikibrain": _adapter("wikibrain"),
        "brainconnect": _adapter("brainconnect"),
    }
    selected = MemoryRouter(MemoryConfig(), adapters).select_backends(
        "implementation_constraints")
    assert selected == ["wikibrain"]  # exact match, selected once


# ------------------------------------------------------------------ bootstrap
def test_bootstrap_env_registers_brainconnect(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAINCONNECT_URL", "http://localhost:8787")
    monkeypatch.delenv("WIKIBRAIN_URL", raising=False)
    monkeypatch.delenv("COGNEE_URL", raising=False)
    monkeypatch.delenv("GRAPHITI_URL", raising=False)
    monkeypatch.setenv("AGENTCONNECT_MEMORY_CONFIG", str(tmp_path / "missing.yaml"))
    adapters, _config = memory_from_env()
    assert set(adapters) == {"brainconnect"}
    adapter = adapters["brainconnect"]
    assert isinstance(adapter, WikiBrainMemoryAdapter)
    assert adapter.backend_name == "brainconnect"  # reports the configured name


def test_bootstrap_yaml_declared_brainconnect(monkeypatch, tmp_path):
    config_file = tmp_path / "memory.yaml"
    config_file.write_text(
        "memory:\n"
        "  enabled: true\n"
        "  trusted_authority: brainconnect\n"
        "  backends:\n"
        "    brainconnect:\n"
        "      enabled: true\n"
        "      base_url: http://localhost:8787\n",
        encoding="utf-8",
    )
    for var in ("WIKIBRAIN_URL", "BRAINCONNECT_URL", "COGNEE_URL", "GRAPHITI_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENTCONNECT_MEMORY_CONFIG", str(config_file))
    adapters, config = memory_from_env()
    assert set(adapters) == {"brainconnect"}
    assert config.trusted_authority == "brainconnect"


# ------------------------------------------------------------------ deny-lists
def test_the_denials_follow_the_rename():
    for action in ("brainconnect_promote", "brainconnect_admin"):
        assert action in AGENT_FORBIDDEN_ACTIONS
        assert action in NEVER_TOKEN_ACTIONS
    # The old spellings stay denied for as long as either can appear anywhere.
    for action in ("wikibrain_promote", "wikibrain_admin"):
        assert action in AGENT_FORBIDDEN_ACTIONS
        assert action in NEVER_TOKEN_ACTIONS
    assert "brainconnect_promote" in DENIED_MCP_TOOLS
    assert "wikibrain_promote" in DENIED_MCP_TOOLS


# ------------------------------------------------------------------ runtime sink
def test_memory_sink_prefers_the_brainconnect_cli(monkeypatch):
    import shutil

    from agentconnect.runtime.memory import McpStdioMemorySink

    monkeypatch.setattr(shutil, "which",
                        lambda cmd: "/usr/bin/brainconnect" if cmd == "brainconnect" else None)
    assert McpStdioMemorySink()._command == "brainconnect"


def test_memory_sink_falls_back_to_the_wiki_cli(monkeypatch):
    import shutil

    from agentconnect.runtime.memory import McpStdioMemorySink

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    assert McpStdioMemorySink()._command == "wiki"
    # An explicit command is never second-guessed.
    assert McpStdioMemorySink(command="wiki")._command == "wiki"
