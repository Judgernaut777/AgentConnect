"""Scope expansion in ContextBuilder.

Before this, every recall asked for exactly `task:<id>`. That is almost no
claims: durable knowledge — "this repo pins sqlite WAL", "Qwen is weak at auth
review", "we never ship on Fridays" — is filed at repo, project, model, or global
scope. A task-only request misses all of it and returns a pack that looks fine.

Two rules the tests exist to hold:

1. **A profile asks at the scopes it is *for*.** A bounded worker never sees
   manager-scoped claims; `model_performance` is the only profile that reaches
   worker/model scope, because it is the only one that is *about* them.
2. **An unresolvable scope is dropped and reported, never sent empty.** `repo:`
   matches nothing, and looks exactly like a repo that had nothing to say.
"""

import pytest

from agentconnect.core import (
    AgentConnectService,
    CogneeMemoryAdapter,
    CreateTaskRequest,
    EchoWorker,
    GraphitiMemoryAdapter,
    MemoryConfig,
    WikiBrainMemoryAdapter,
)
from agentconnect.core.context import (
    GLOBAL_SCOPE_ID,
    PROFILES,
    SCOPE_ORDER,
    VALID_SCOPE_TYPES,
    MemoryConfig as Config,
    ProfileConfig,
    resolve_scopes,
)

CLAIM = {"text": "Repo pins sqlite WAL.", "status": "promoted", "confidence": "verified",
         "source_id": "claim_1", "trusted": True}


def spy():
    """Records the scopes each backend was asked at."""
    seen: dict[str, list[dict]] = {}

    def make(name, response):
        def transport(method, url, payload):
            if url.endswith(("/recall", "/search")):
                seen[name] = (payload or {}).get("scopes")
            return response
        return transport

    return seen, make


def build(tmp_path, metadata=None, default_scopes=None, current_manager=None):
    seen, make = spy()
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()],
        memory_backends={
            "wikibrain": WikiBrainMemoryAdapter(
                transport=make("wikibrain", {"items": [CLAIM]})),
            "cognee": CogneeMemoryAdapter(transport=make("cognee", {"results": []})),
            "graphiti": GraphitiMemoryAdapter(transport=make("graphiti", {"facts": []})),
        },
        memory_config=MemoryConfig(default_scopes=dict(default_scopes or {})),
    )
    task = svc.create_task(CreateTaskRequest(
        title="Refactor auth", goal="dedupe expiry", metadata=dict(metadata or {})))
    if current_manager:
        svc.claim_task(task.id, current_manager)
    return svc, task, seen


# ------------------------------------------------------------- the vocabulary
def test_global_carries_no_scope_id():
    """WikiBrain's `Scope.__post_init__` raises on `global` with an id, and on any
    other type without one. A synthetic id would make the whole recall throw, and
    ContextBuilder would degrade that into an empty pack — silently."""
    assert GLOBAL_SCOPE_ID == ""

    resolution = resolve_scopes(PROFILES["hard_policy"], _detail("task_1"), Config())
    scope = resolution.scopes[0]
    assert scope.scope_type == "global" and scope.scope_id == ""
    assert resolution.as_strings()[0] == "global"  # rendered bare, not `global:`


def test_every_declared_scope_is_in_the_authoritys_vocabulary():
    for profile in PROFILES.values():
        assert set(profile.scopes) <= VALID_SCOPE_TYPES


def test_an_unknown_scope_type_fails_loudly_rather_than_degrading():
    bad = ProfileConfig(["wikibrain"], scopes=("global", "galaxy"))
    with pytest.raises(ValueError, match="unknown scope type 'galaxy'"):
        resolve_scopes(bad, _detail("task_1"), Config())


class _Task:
    def __init__(self, task_id, metadata=None, current_manager=None):
        self.id = task_id
        self.metadata = metadata or {}
        self.current_manager = current_manager


class _Detail:
    def __init__(self, task):
        self.task = task


def _detail(task_id, metadata=None, current_manager=None):
    return _Detail(_Task(task_id, metadata, current_manager))


# --------------------------------------------------------------- resolution
def test_ids_come_from_task_metadata_first_then_config_defaults():
    config = Config(default_scopes={"project": "fascia", "repo": "fallback-repo"})
    detail = _detail("task_1", {"repo_id": "mcp-agentconnect"})

    resolution = resolve_scopes(PROFILES["manager_brief"], detail, config)
    assert resolution.as_strings() == [
        "global", "project:fascia", "repo:mcp-agentconnect", "task:task_1"]
    assert resolution.missing == []


def test_scopes_are_ordered_broadest_first():
    config = Config(default_scopes={"project": "fascia", "repo": "r"})
    detail = _detail("task_1", current_manager="claude")
    kinds = [s.scope_type for s in
             resolve_scopes(PROFILES["manager_brief"], detail, config).scopes]
    assert kinds == sorted(kinds, key=SCOPE_ORDER.index)
    assert kinds[0] == "global" and kinds[-1] == "manager"


def test_an_unresolvable_scope_is_dropped_and_reported_never_sent_empty():
    resolution = resolve_scopes(PROFILES["manager_brief"], _detail("task_1"), Config())

    assert resolution.as_strings() == ["global", "task:task_1"]
    assert all(s.scope_id or s.scope_type == "global" for s in resolution.scopes)
    assert resolution.missing == ["project", "repo"]


def test_an_unclaimed_task_missing_its_manager_scope_is_not_reported():
    """A task nobody has claimed has no manager scope. That is unremarkable; a repo
    we never recorded is knowledge we are failing to reach."""
    resolution = resolve_scopes(PROFILES["manager_brief"], _detail("task_1"), Config())
    assert "manager" not in resolution.missing
    assert not any(s.scope_type == "manager" for s in resolution.scopes)


def test_an_explicit_manager_id_beats_the_tasks_current_manager():
    detail = _detail("task_1", current_manager="codex")
    resolved = resolve_scopes(PROFILES["manager_brief"], detail, Config(),
                              manager_id="claude")
    assert "manager:claude" in resolved.as_strings()

    inherited = resolve_scopes(PROFILES["manager_brief"], detail, Config())
    assert "manager:codex" in inherited.as_strings()


# --------------------------------------------------------- profile scope sets
def test_a_worker_brief_never_asks_at_manager_scope(tmp_path):
    svc, task, seen = build(tmp_path, current_manager="claude")
    pack = svc.get_task_context_pack(task.id, profile="worker_brief")

    assert pack.scopes_queried == ["global", f"task:{task.id}"]
    assert not any(s["scope_type"] == "manager" for s in seen["wikibrain"])


def test_a_manager_brief_asks_at_manager_scope(tmp_path):
    svc, task, seen = build(tmp_path, current_manager="claude")
    pack = svc.get_task_context_pack(task.id, profile="manager_brief")
    assert "manager:claude" in pack.scopes_queried


def test_model_performance_is_the_only_profile_that_reaches_worker_and_model(tmp_path):
    svc, task, _ = build(tmp_path)
    pack = svc.get_task_context_pack(
        task.id, profile="model_performance", worker_id="local_qwen", model_id="qwen3-30b")

    assert "worker:local_qwen" in pack.scopes_queried
    assert "model:qwen3-30b" in pack.scopes_queried

    for name, cfg in PROFILES.items():
        if name != "model_performance":
            assert "worker" not in cfg.scopes and "model" not in cfg.scopes, name


def test_a_worker_or_model_id_is_ignored_by_profiles_that_do_not_declare_it(tmp_path):
    svc, task, _ = build(tmp_path)
    pack = svc.get_task_context_pack(
        task.id, profile="manager_brief", worker_id="local_qwen", model_id="qwen3-30b")
    assert not any(s.startswith(("worker:", "model:")) for s in pack.scopes_queried)


def test_model_performance_without_a_worker_reports_the_gap(tmp_path):
    svc, task, _ = build(tmp_path)
    pack = svc.get_task_context_pack(task.id, profile="model_performance")

    assert not any(s.startswith("worker:") for s in pack.scopes_queried)
    assert any("worker" in w and "cannot surface" in w for w in pack.warnings)


def test_project_evolution_and_hard_policy_never_narrow_to_one_task(tmp_path):
    svc, task, _ = build(tmp_path, default_scopes={"project": "fascia", "repo": "r"})
    for profile in ("project_evolution", "hard_policy", "broad_project_rag"):
        pack = svc.get_task_context_pack(task.id, profile=profile)
        assert f"task:{task.id}" not in pack.scopes_queried, profile
        assert "project:fascia" in pack.scopes_queried, profile


# ----------------------------------------------------- every backend is scoped
def test_all_three_backends_receive_the_same_scopes(tmp_path):
    """Cognee and Graphiti previously dropped scopes on the floor, so a repo-scoped
    question got answered out of another project's documents — and the answer read
    exactly like a relevant one."""
    svc, task, seen = build(tmp_path, default_scopes={"project": "fascia",
                                                      "repo": "mcp-agentconnect"})
    svc.get_task_context_pack(task.id, profile="manager_brief")

    expected = [
        {"scope_type": "global", "scope_id": ""},
        {"scope_type": "project", "scope_id": "fascia"},
        {"scope_type": "repo", "scope_id": "mcp-agentconnect"},
        {"scope_type": "task", "scope_id": task.id},
    ]
    assert seen["wikibrain"] == expected
    assert seen["cognee"] == expected
    assert seen["graphiti"] == expected


# ------------------------------------------------------------- observability
def test_the_pack_and_the_worker_push_both_carry_the_scopes_queried(tmp_path):
    from agentconnect.core.models import SubtaskRequest

    svc, task, _ = build(tmp_path, metadata={"repo_id": "mcp-agentconnect"})
    pack = svc.get_task_context_pack(task.id, profile="worker_brief")
    assert pack.scopes_queried == ["global", "repo:mcp-agentconnect", f"task:{task.id}"]

    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    svc.attach_context_to_subtask(subtask.id, pack)
    stored = svc.get_subtask(subtask.id).subtask.metadata["context_pack"]
    assert stored["scopes_queried"] == pack.scopes_queried


def test_a_missing_scope_warning_names_the_fix(tmp_path):
    svc, task, _ = build(tmp_path)
    warning = next(w for w in svc.get_task_context_pack(task.id).warnings
                   if "cannot surface" in w)
    assert "project, repo" in warning
    assert "task.metadata" in warning and "memory.default_scopes" in warning


def test_configuring_default_scopes_silences_the_warning(tmp_path):
    svc, task, _ = build(tmp_path, default_scopes={"project": "fascia", "repo": "r"})
    pack = svc.get_task_context_pack(task.id)
    assert not any("cannot surface" in w for w in pack.warnings)
    assert pack.scopes_queried == ["global", "project:fascia", "repo:r", f"task:{task.id}"]


def test_scopes_survive_a_yaml_round_trip():
    config = Config.from_dict({"memory": {
        "default_scopes": {"project": "fascia", "repo": "mcp-agentconnect"},
        "profiles": {"worker_brief": {"scopes": ["global", "repo", "task"]}},
    }})
    assert config.default_scopes == {"project": "fascia", "repo": "mcp-agentconnect"}
    assert config.profile("worker_brief").scopes == ("global", "repo", "task")
    # An unnamed profile keeps its built-in scopes.
    assert config.profile("manager_brief").scopes == PROFILES["manager_brief"].scopes
