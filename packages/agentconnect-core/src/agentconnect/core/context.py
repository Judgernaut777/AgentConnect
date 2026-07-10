"""ContextBuilder, MemoryRouter, MemoryRanker (memory-stack spec §5–§10).

> AgentConnect controls access. WikiBrain controls trust. Cognee improves breadth.
> Graphiti improves temporal reasoning.

This module is where "controls access" lives. Managers and workers never touch a
memory backend; they get one bounded, ranked, source-labeled pack from here.

**How the two kinds of agent actually receive memory**

* *Managers* (Claude Code, Codex, Linear Agent — proprietary, unmodifiable) **pull**:
  they call the `get_task_context_pack` MCP tool. They cannot reach WikiBrain,
  Cognee, or Graphiti, because only the AgentConnect MCP server is mounted for
  them. This is the entire reason the MCP adapter exists.
* *Workers* (bounded, often no MCP client at all) get memory **pushed**: the
  `recall_context` activity builds a `worker_brief` pack and attaches it to the
  subtask before `run_worker` runs, so the harness reads it from
  `subtask.metadata["context_pack"]`.

Neither ever promotes a fact. Trust is conferred by a human in WikiBrain.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel

from .. import safety
from .memory import (
    BROAD_RETRIEVAL,
    DEFAULT_MAX_ITEMS,
    LEDGER,
    TEMPORAL_GRAPH,
    TRUSTED_AUTHORITY,
    MemoryAdapter,
    MemoryItem,
    MemoryScope,
    RecallPack,
    RecallRequest,
    label,
)

_log = logging.getLogger(__name__)

WIKIBRAIN = "wikibrain"
COGNEE = "cognee"
GRAPHITI = "graphiti"

# ------------------------------------------------------------------- scopes
#: The scope vocabulary a claim can be filed under. A recall that asks only for
#: `task:<id>` sees only claims somebody filed against that one task — which is
#: almost none of them. Durable knowledge ("this repo pins sqlite WAL", "Qwen is
#: weak at auth review") lives at repo, project, model, or global scope, and a
#: task-only request silently misses all of it.
GLOBAL = "global"
PROJECT = "project"
REPO = "repo"
TASK = "task"
MANAGER = "manager"
WORKER = "worker"
MODEL = "model"

#: Every scope kind, broadest first. Used to order the scopes on a request so a
#: backend that honors ordering sees the widest context before the narrowest.
SCOPE_ORDER: tuple[str, ...] = (GLOBAL, PROJECT, REPO, TASK, MANAGER, WORKER, MODEL)

#: **`global` takes no scope_id.** This is the trusted authority's vocabulary, not
#: ours: WikiBrain's `Scope.__post_init__` raises on `global` with an id, and on
#: every other type *without* one. A synthetic id like `"*"` therefore makes the
#: whole recall throw — which `ContextBuilder` would dutifully degrade into an
#: empty pack and a warning, losing every trusted claim in silence.
GLOBAL_SCOPE_ID = ""

#: WikiBrain's closed vocabulary (`wiki.scopes.SCOPE_TYPES`). We use a subset;
#: sending anything outside it is a caller bug, and it should surface here rather
#: than as a degraded pack.
VALID_SCOPE_TYPES: frozenset[str] = frozenset({
    GLOBAL, "user", PROJECT, REPO, TASK, MANAGER, WORKER, MODEL, "tool",
})

#: Task metadata keys the resolver reads. Set them at `create_task` time, or let
#: `MemoryConfig.default_scopes` supply a deployment-wide fallback.
PROJECT_KEY = "project_id"
REPO_KEY = "repo_id"


@dataclass
class ProfileConfig:
    backends: list[str]
    max_items: int = DEFAULT_MAX_ITEMS
    #: Whether the pack carries the full deterministic handoff. A worker gets the
    #: subtask and its constraints, never the manager's debate (§10).
    include_handoff: bool = True
    #: Locked decisions and task constraints are ledger truth, always allowed.
    include_ledger: bool = True
    include_superseded: bool = False
    #: Which scope kinds this profile asks for. A bounded worker never gets
    #: manager-scoped claims; `model_performance` is the only profile that reaches
    #: worker/model scope, because it is the only one that is *about* them.
    scopes: tuple[str, ...] = (GLOBAL, PROJECT, REPO, TASK)


#: Spec §7 + §14. A profile names *what the caller is for*, and that determines
#: which backends are even asked.
PROFILES: dict[str, ProfileConfig] = {
    "manager_brief": ProfileConfig(
        [WIKIBRAIN, COGNEE, GRAPHITI], 8,
        scopes=(GLOBAL, PROJECT, REPO, TASK, MANAGER)),
    # No manager scope: a bounded worker does not inherit the manager's history.
    "worker_brief": ProfileConfig(
        [WIKIBRAIN, COGNEE], 5, include_handoff=False,
        scopes=(GLOBAL, PROJECT, REPO, TASK)),
    "reviewer_brief": ProfileConfig(
        [WIKIBRAIN, GRAPHITI], 8,
        scopes=(GLOBAL, PROJECT, REPO, TASK, MANAGER)),
    "implementation_constraints": ProfileConfig(
        [WIKIBRAIN], 6, include_handoff=False,
        scopes=(GLOBAL, PROJECT, REPO, TASK)),
    "known_failures": ProfileConfig(
        [WIKIBRAIN, GRAPHITI], 8,
        scopes=(GLOBAL, PROJECT, REPO, TASK)),
    # The only profile that is *about* a worker or a model, so the only one that
    # asks at those scopes. Task scope stays: "this model failed on this task".
    "model_performance": ProfileConfig(
        [WIKIBRAIN, GRAPHITI], 8, include_handoff=False,
        scopes=(GLOBAL, PROJECT, TASK, WORKER, MODEL)),
    # Roomier than the rest: superseded claims rank last, so at a budget of 8 the
    # ledger and the live claims would crowd out the very history this asks for.
    "project_evolution": ProfileConfig(
        [WIKIBRAIN, GRAPHITI], 10, include_superseded=True,
        scopes=(GLOBAL, PROJECT, REPO)),
    "broad_project_rag": ProfileConfig(
        [COGNEE], 8, include_handoff=False,
        scopes=(GLOBAL, PROJECT, REPO)),
    "hard_policy": ProfileConfig(
        [WIKIBRAIN], 6, include_handoff=False,
        scopes=(GLOBAL, PROJECT, REPO)),
}

DEFAULT_PROFILE = "manager_brief"


@dataclass
class MemoryDefaults:
    trusted_only: bool = True
    include_pending: bool = False
    include_superseded: bool = False
    max_items: int = DEFAULT_MAX_ITEMS


@dataclass
class MemoryConfig:
    enabled: bool = True
    trusted_authority: str = WIKIBRAIN
    defaults: MemoryDefaults = field(default_factory=MemoryDefaults)
    profiles: dict[str, ProfileConfig] = field(default_factory=lambda: dict(PROFILES))
    #: Hard preferences that affect routing/privacy/cost/safety (§3). These are
    #: policy, not memory: they are never ranked away and never expire.
    hard_policies: list[str] = field(default_factory=list)
    #: Deployment-wide fallbacks for scope ids a task does not name itself,
    #: e.g. ``{"project": "fascia", "repo": "mcp-agentconnect"}``.
    default_scopes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryConfig":
        memory = raw.get("memory", raw) or {}
        defaults_raw = memory.get("defaults") or {}
        profiles = dict(PROFILES)
        for name, spec in (memory.get("profiles") or {}).items():
            base = profiles.get(name, ProfileConfig([WIKIBRAIN]))
            profiles[name] = ProfileConfig(
                backends=list(spec.get("backends", base.backends)),
                max_items=int(spec.get("max_items", base.max_items)),
                include_handoff=bool(spec.get("include_handoff", base.include_handoff)),
                include_ledger=bool(spec.get("include_ledger", base.include_ledger)),
                include_superseded=bool(
                    spec.get("include_superseded", base.include_superseded)
                ),
                scopes=tuple(spec.get("scopes", base.scopes)),
            )
        return cls(
            enabled=bool(memory.get("enabled", True)),
            trusted_authority=str(memory.get("trusted_authority", WIKIBRAIN)),
            defaults=MemoryDefaults(
                trusted_only=bool(defaults_raw.get("trusted_only", True)),
                include_pending=bool(defaults_raw.get("include_pending", False)),
                include_superseded=bool(defaults_raw.get("include_superseded", False)),
                max_items=int(defaults_raw.get("max_items", DEFAULT_MAX_ITEMS)),
            ),
            profiles=profiles,
            hard_policies=list(memory.get("hard_policies", [])),
            default_scopes=dict(memory.get("default_scopes") or {}),
        )

    def profile(self, name: str) -> ProfileConfig:
        return self.profiles.get(name) or self.profiles[DEFAULT_PROFILE]


class ContextPack(BaseModel):
    """Ledger truth and external context, side by side and never merged.

    ``memory_is_external_context`` is the contract that stops a Cognee search hit
    from being read as a recorded decision.
    """

    model_config = {"arbitrary_types_allowed": True}

    task_id: str
    profile: str
    handoff: Optional[Any] = None
    memory: RecallPack
    backends_queried: list[str] = []
    #: `["global", "project:fascia", "repo:mcp-agentconnect", "task:task_1"]`.
    #: Visible so a caller can see *why* a claim did or did not surface.
    scopes_queried: list[str] = []
    warnings: list[str] = []
    memory_is_external_context: bool = True


#: Back-compat name from the previous handoff. Same object.
TaskContextPack = ContextPack


@dataclass
class ScopeResolution:
    """The scopes a request will carry, plus the ones it wanted and could not find.

    `missing` is not an error — a task with no repo is a normal task. It is a
    *warning*, because "no repo-scoped claim surfaced" and "this deployment never
    told us which repo" are indistinguishable from inside the pack, and the second
    is the one that silently loses knowledge.
    """

    scopes: list[MemoryScope] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def as_strings(self) -> list[str]:
        # `global`, not `global:` — the authority's own rendering.
        return [
            s.scope_type if not s.scope_id else f"{s.scope_type}:{s.scope_id}"
            for s in self.scopes
        ]


def resolve_scopes(
    profile_cfg: ProfileConfig,
    detail: Any,
    config: MemoryConfig,
    manager_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> ScopeResolution:
    """Turn a profile's declared scope kinds into concrete, ordered scopes.

    Ids come from the task first (`metadata["project_id"]`, `metadata["repo_id"]`),
    then from `MemoryConfig.default_scopes`. A scope kind that resolves to nothing
    is dropped and reported, never sent as an empty id — `repo:` matches nothing,
    but it looks like it matched nothing *because there was nothing to find*.
    """
    task = detail.task
    metadata = task.metadata or {}
    unknown = set(profile_cfg.scopes) - VALID_SCOPE_TYPES
    if unknown:
        # Filtering it out silently would be the same bug one layer up: a profile
        # that asks at a scope nobody indexes returns a plausible, narrower pack.
        raise ValueError(
            f"unknown scope type {sorted(unknown)[0]!r} in profile config; "
            f"expected one of {', '.join(sorted(VALID_SCOPE_TYPES))}"
        )
    wanted = [k for k in SCOPE_ORDER if k in profile_cfg.scopes]

    resolved: dict[str, Optional[str]] = {
        GLOBAL: GLOBAL_SCOPE_ID,
        PROJECT: metadata.get(PROJECT_KEY) or config.default_scopes.get(PROJECT),
        REPO: metadata.get(REPO_KEY) or config.default_scopes.get(REPO),
        TASK: task.id,
        MANAGER: manager_id or task.current_manager,
        WORKER: worker_id,
        MODEL: model_id,
    }

    scopes: list[MemoryScope] = []
    missing: list[str] = []
    for kind in wanted:
        if kind == GLOBAL:
            scopes.append(MemoryScope(GLOBAL, GLOBAL_SCOPE_ID))
            continue
        value = resolved.get(kind)
        if value:
            scopes.append(MemoryScope(kind, str(value)))
        elif kind != MANAGER:
            # An unclaimed task having no manager scope is unremarkable; a repo or
            # project we simply never recorded is knowledge we are failing to reach.
            missing.append(kind)
    return ScopeResolution(scopes=scopes, missing=missing)


def _scope_warning(missing: list[str], profile: str) -> list[str]:
    if not missing:
        return []
    return [
        f"{profile}: no {', '.join(missing)} scope is known for this task, so claims "
        f"filed at that scope cannot surface (set task.metadata or memory.default_scopes)"
    ]


class MemoryRouter:
    """Which backends does *this profile* get to ask? (§7)

    `implementation_constraints` asks only the trusted authority. A worker never
    reaches the temporal graph. Narrowing the question is how the pack stays
    bounded before ranking ever runs.
    """

    def __init__(self, config: MemoryConfig, adapters: dict[str, MemoryAdapter]) -> None:
        self.config = config
        self.adapters = adapters

    def select_backends(
        self, profile: str, task_id: Optional[str] = None, query: Optional[str] = None
    ) -> list[str]:
        if not self.config.enabled:
            return []
        wanted = self.config.profile(profile).backends
        selected = [name for name in wanted if name in self.adapters]
        if selected:
            return selected
        # A deployment with a backend the profiles do not name (a StaticMemoryAdapter
        # in tests, a bespoke engine) still gets queried rather than silently ignored.
        return [n for n in sorted(self.adapters) if n != "none"]


_WORD = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    return _WORD.sub(" ", text.lower()).strip()


class MemoryRanker:
    """Merge, dedupe, and order results from backends that disagree (§8).

    Authority order is fixed and boring on purpose. A retrieval engine surfacing
    a sentence three times must never outrank a librarian promoting it once.
    """

    #: Lower is more authoritative.
    LEDGER_RANK = 0
    WIKIBRAIN_VERIFIED = 1
    WIKIBRAIN_PROMOTED = 2
    GRAPHITI_TIED_TO_PROMOTED = 3
    COGNEE_BROAD = 4
    PENDING_OR_UNKNOWN = 5

    def authority(self, item: MemoryItem) -> int:
        role = (item.metadata or {}).get("role", BROAD_RETRIEVAL)
        if role == LEDGER:
            return self.LEDGER_RANK
        if item.status == "pending":
            return self.PENDING_OR_UNKNOWN
        if role == TRUSTED_AUTHORITY and item.status == "promoted":
            # `promoted` is not authority; `trusted` is. A claim the authority
            # promoted but declined to trust (an open contradiction) must not rank
            # above a search hit, let alone above an undisputed claim.
            if not (item.metadata or {}).get("trusted", False):
                return self.PENDING_OR_UNKNOWN
            return (
                self.WIKIBRAIN_VERIFIED if item.confidence == "verified"
                else self.WIKIBRAIN_PROMOTED
            )
        if role == TEMPORAL_GRAPH:
            # Only relationships anchored to a promoted claim carry weight; a bare
            # graph edge is no better than a search hit.
            return (
                self.GRAPHITI_TIED_TO_PROMOTED if item.source_id
                else self.PENDING_OR_UNKNOWN
            )
        if role == BROAD_RETRIEVAL and item.source_id:
            return self.COGNEE_BROAD
        return self.PENDING_OR_UNKNOWN

    def merge_and_rank(
        self, packs: list[RecallPack], profile: str, max_items: int
    ) -> RecallPack:
        best: dict[str, MemoryItem] = {}
        order: list[str] = []
        warnings: list[str] = []
        backends: list[str] = []

        for pack in packs:
            warnings.extend(pack.warnings)
            if pack.backend not in backends:
                backends.append(pack.backend)
            for item in pack.items:
                key = _normalize(item.text)
                if not key:
                    continue
                if key not in best:
                    best[key] = item
                    order.append(key)
                    continue
                incumbent = best[key]
                if self.authority(item) < self.authority(incumbent):
                    # The same fact from a more authoritative backend replaces the
                    # weaker copy, but keeps the corroborating source visible.
                    item.metadata = dict(item.metadata or {})
                    item.metadata["also_seen_in"] = sorted(
                        {*(incumbent.metadata or {}).get("also_seen_in", []),
                         (incumbent.metadata or {}).get("backend", "unknown")}
                    )
                    best[key] = item
                else:
                    incumbent.metadata = dict(incumbent.metadata or {})
                    incumbent.metadata["also_seen_in"] = sorted(
                        {*(incumbent.metadata or {}).get("also_seen_in", []),
                         (item.metadata or {}).get("backend", "unknown")}
                    )

        items = [best[k] for k in order]
        items.sort(key=lambda i: (self.authority(i), -float(
            (i.metadata or {}).get("score") or 0.0
        )))
        return RecallPack(
            profile=profile, query="", items=items[: max(0, max_items)],
            backend="+".join(backends) or "none",
            warnings=list(dict.fromkeys(warnings)),
        )


class ContextBuilder:
    """Builds the one bounded pack a manager or worker is allowed to see (§6)."""

    def __init__(
        self,
        service: Any,
        adapters: dict[str, MemoryAdapter],
        config: Optional[MemoryConfig] = None,
        ranker: Optional[MemoryRanker] = None,
        safety_enabled: bool = True,
    ) -> None:
        self.service = service
        self.adapters = adapters
        self.config = config or MemoryConfig()
        self.router = MemoryRouter(self.config, adapters)
        self.ranker = ranker or MemoryRanker()
        #: Scan recalled items before they leave for an agent. See `_apply_safety`.
        self.safety_enabled = safety_enabled

    # ------------------------------------------------------------- ledger
    def _ledger_items(self, detail: Any) -> list[MemoryItem]:
        """Locked decisions and hard policy: the only facts that outrank memory."""
        items: list[MemoryItem] = []
        for policy in self.config.hard_policies:
            items.append(label(MemoryItem(
                text=policy, status="promoted", confidence="verified",
                source_id="agentconnect_config",
                metadata={"kind": "hard_policy"},
            ), "agentconnect", LEDGER))
        for constraint in detail.constraints:
            items.append(label(MemoryItem(
                text=constraint.text, status="promoted", confidence="verified",
                source_id=constraint.id, metadata={"kind": "constraint"},
            ), "agentconnect", LEDGER))
        for decision in detail.decisions:
            if decision.locked and decision.superseded_by is None:
                text = decision.decision
                if decision.rationale:
                    text = f"{text} ({decision.rationale})"
                items.append(label(MemoryItem(
                    text=text, status="promoted", confidence="verified",
                    source_id=decision.id, metadata={"kind": "locked_decision"},
                ), "agentconnect", LEDGER))
        return items

    # ------------------------------------------------------------- backends
    def _recall(
        self, name: str, request: RecallRequest, warnings: list[str]
    ) -> Optional[RecallPack]:
        adapter = self.adapters[name]
        try:
            pack = adapter.recall(request)
        except Exception as exc:
            # §11: a memory outage degrades the pack, it never fails the caller.
            _log.warning("memory backend %r recall failed: %s", name, exc)
            warnings.append(f"{name} recall failed: {exc}")
            return None
        role = getattr(adapter, "role", BROAD_RETRIEVAL)
        for item in pack.items:
            label(item, adapter.backend_name, role)
        return pack

    def build_context_pack(
        self,
        task_id: str,
        profile: str = DEFAULT_PROFILE,
        query: Optional[str] = None,
        max_items: Optional[int] = None,
        manager_id: Optional[str] = None,
        include_pending: bool = False,
        worker_id: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> ContextPack:
        profile_cfg = self.config.profile(profile)
        budget = max_items or profile_cfg.max_items
        warnings: list[str] = []

        detail = self.service.get_task(task_id)
        handoff = (
            self.service.get_handoff_summary(task_id, manager_id)
            if profile_cfg.include_handoff else None
        )
        query = query or f"{detail.task.title}\n{detail.task.goal}".strip()

        resolution = resolve_scopes(
            profile_cfg, detail, self.config,
            manager_id=manager_id, worker_id=worker_id, model_id=model_id,
        )
        warnings.extend(_scope_warning(resolution.missing, profile))

        packs: list[RecallPack] = []
        if profile_cfg.include_ledger:
            ledger_items = self._ledger_items(detail)
            if ledger_items:
                packs.append(RecallPack(
                    profile=profile, query=query, items=ledger_items, backend="agentconnect",
                ))

        backends = self.router.select_backends(profile, task_id, query) if self.config.enabled \
            else []
        if not self.config.enabled:
            warnings.append("memory is disabled; context is task state only")

        for name in backends:
            adapter = self.adapters[name]
            is_authority = getattr(adapter, "role", None) == TRUSTED_AUTHORITY
            request = RecallRequest(
                query=query, task_id=task_id, profile=profile,  # type: ignore[arg-type]
                scopes=list(resolution.scopes),
                max_items=budget,
                # Only the trusted authority is asked to enforce trust: a retrieval
                # engine has no notion of promotion, and filtering it on
                # `trusted_only` would silently return nothing.
                trusted_only=is_authority and not include_pending,
                include_pending=include_pending,
                include_superseded=profile_cfg.include_superseded,
            )
            pack = self._recall(name, request, warnings)
            if pack is not None:
                packs.append(pack)

        merged = self.ranker.merge_and_rank(packs, profile, budget)
        merged.query = query
        merged.items, safety_warnings = self._apply_safety(merged.items)
        merged.warnings = list(dict.fromkeys(merged.warnings + warnings + safety_warnings))
        return ContextPack(
            task_id=task_id, profile=profile, handoff=handoff, memory=merged,
            backends_queried=backends, scopes_queried=resolution.as_strings(),
            warnings=merged.warnings,
        )

    # ------------------------------------------------------------- safety
    def _apply_safety(self, items: list[MemoryItem]) -> tuple[list[MemoryItem], list[str]]:
        """Scan recalled items before an agent sees them (safety `context_output`).

        This is the surface prompt injection exists to attack: text retrieved from a
        backend, about to be handed to an agent as context. A secret is redacted; a
        high-confidence injection is withheld entirely.

        **Nothing is dropped silently.** A withheld item returns a warning naming the
        count, because a context pack that quietly got shorter is indistinguishable
        from one that had nothing to say — and the second is the reading an agent
        will make.

        Only *recalled memory* is scanned. Ledger truth — locked decisions, hard
        policy — is AgentConnect's own record, and redacting a decision would corrupt
        the thing the audit relies on. Scanning attempts and decisions is a separate,
        later surface (`attempt_decision_notes`).

        Memory failure degrades a pack; a safety failure withholds an item. The two
        are different: we know how to proceed without a memory backend, and we do not
        know what is inside text we could not read.
        """
        if not self.safety_enabled or not items:
            return items, []

        try:
            batch = safety.scan_items(
                [safety.SafetyItem(id=str(i), text=item.text)
                 for i, item in enumerate(items)],
                policy=safety.CONTEXT_OUTPUT,
            )
        except Exception as exc:  # noqa: BLE001 — never hand back unscanned context
            _log.warning("context safety scan failed: %s", exc)
            return [], [f"all {len(items)} context items were withheld: "
                        f"AgentConnect safety scanning failed ({exc})."]

        kept: list[MemoryItem] = []
        for index, item in enumerate(items):
            result = batch.results[str(index)]
            if result.withheld:
                continue
            if result.redacted:
                item.text = result.redacted_content
            if result.labels:
                item.metadata = {**(item.metadata or {}), "safety_labels": result.labels}
            kept.append(item)
        return kept, batch.warnings()
