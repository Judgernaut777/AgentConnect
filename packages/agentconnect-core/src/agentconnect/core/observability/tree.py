"""`GET /observe/tree` assembly (docs/EVENT_BUS.md §8, Part 2 of the ecosystem
observability spec).

A single read-time aggregation over already-indexed storage reads — task ->
manager_session/subtask/review -> worker_run, wired by delegation id exactly
like `AgentConnectService.agent_tree` (which this module deliberately does not
touch: that method is a stable, narrower surface consumed elsewhere). No new
tables, no caching, no live process introspection: everything here is a
reconstruction from the ledger, honest about what it does not know (a
`current_tool` is "the last authorized tool", never a live invocation; a
subtask's `lease` is null in this deployment because the federated
`WorkQueue`'s ticket/fence concept lives in a different service and is not
wired to `AgentConnectService` — documented, not faked).

Privacy is enforced HERE, at read/serialization time (docs/EVENT_BUS.md §6):
storage is never mutated, and an unparseable/unknown privacy tier is treated
as the strictest tier (`secret_sensitive`) rather than passed through — the
one asymmetric failure mode a redaction boundary is allowed, because leaking
by default is the only way a redaction bug could put content on the wire.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..models import (
    PRIVACY_STRICTNESS,
    PrivacyTier,
    ReviewStatus,
    SubtaskStatus,
    TaskFilters,
    TERMINAL_TASK_STATUSES,
    strictest,
)

Redactor = Callable[[str], tuple]

#: Terminal-ness for entities whose model has no dedicated frozen-timestamp
#: column of their own (subtasks/reviews use `updated_at`; sessions/runs carry
#: an explicit `ended_at`/`finished_at` which is used directly instead).
_TERMINAL_SUBTASK = frozenset(
    {SubtaskStatus.succeeded, SubtaskStatus.failed, SubtaskStatus.cancelled}
)
_TERMINAL_REVIEW = frozenset(
    {ReviewStatus.completed, ReviewStatus.rejected, ReviewStatus.cancelled}
)

#: Fields `/observe/tree` withholds or truncates a prompt/command/title to, by
#: privacy tier — the same fail-closed ladder `docs/EVENT_BUS.md` §6 documents.
_TRUNCATE_CHARS = {PrivacyTier.public: 2000, PrivacyTier.public_redacted: 400,
                   PrivacyTier.repo_sensitive: 400}
_WITHHELD_TEXT = {
    PrivacyTier.local_only: "[withheld: local_only]",
    PrivacyTier.secret_sensitive: "[redacted: secret_sensitive]",
}
#: Same threshold `AgentConnectService._subtask_event_meta` already applies at
#: WRITE time to a subtask's title — a `title`/`prompt` field is withheld here
#: at read time using the identical strictness cutoff, so the two agree.
_TITLE_WITHHELD_AT = 3


def _safe_tier(tier: Any) -> PrivacyTier:
    """An enum member already (the normal case, since `Subtask.privacy_tier` is
    pydantic-validated on the way in) passes through. Anything else — a raw
    string from a corrupted/pre-enum row, or an unrecognized value — fails
    CLOSED to the strictest tier rather than being guessed at or passed through
    unredacted."""
    if isinstance(tier, PrivacyTier):
        return tier
    try:
        return PrivacyTier(tier)
    except (ValueError, TypeError):
        return PrivacyTier.secret_sensitive


def _redact_text(text: Optional[str], tier: Any, redact: Redactor) -> Optional[str]:
    """The fail-closed prompt/command ladder (docs/EVENT_BUS.md §6)."""
    if text is None:
        return None
    safe_tier = _safe_tier(tier)
    withheld = _WITHHELD_TEXT.get(safe_tier)
    if withheld is not None:
        return withheld
    try:
        redacted_text, _was = redact(text)
    except Exception:  # noqa: BLE001 — a scanner failure withholds, never leaks
        return "[withheld: redaction failed]"
    limit = _TRUNCATE_CHARS.get(safe_tier, 400)
    if len(redacted_text) > limit:
        suffix = "" if safe_tier == PrivacyTier.public else "…[truncated]"
        return redacted_text[:limit] + suffix
    return redacted_text


def _redact_title(title: str, tier: Any) -> str:
    return "[redacted]" if PRIVACY_STRICTNESS[_safe_tier(tier)] >= _TITLE_WITHHELD_AT else title


def _tokens_and_cost(metrics: dict[str, Any]) -> tuple[Optional[dict], Optional[float]]:
    """Metrics land under whichever key names the worker/harness used
    (`tokens_in`/`tokens_out`/`estimated_cost_usd` for every in-tree worker
    today; `input_tokens`/`output_tokens`/`cost_usd`, incl. nested under a
    `usage` dict, are accepted too — the shape a third-party OpenAI-style
    harness naturally reports). `None` while a run is in flight and has not
    reported yet, never a fabricated zero."""
    if not metrics:
        return None, None
    usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
    tokens_in = metrics.get("tokens_in", metrics.get("input_tokens", usage.get("input_tokens")))
    tokens_out = metrics.get("tokens_out", metrics.get("output_tokens", usage.get("output_tokens")))
    cost = metrics.get("estimated_cost_usd", metrics.get("cost_usd", usage.get("cost_usd")))
    tokens = {"input": tokens_in, "output": tokens_out} if (
        tokens_in is not None or tokens_out is not None
    ) else None
    return tokens, cost


def _artifact_node(a: Any) -> dict[str, Any]:
    return {"id": a.id, "name": a.summary or a.type.value, "created_at": a.created_at}


def _base_node(
    *, kind: str, id_: str, title: str, state: str, privacy_tier: PrivacyTier,
    prompt: Optional[str] = None, model: Optional[str] = None,
    current_tool: Optional[str] = None, tokens: Optional[dict] = None,
    cost_usd: Optional[float] = None, lease: Optional[dict] = None,
    elapsed_s: float = 0.0, started_at: Optional[float] = None,
    actor: Optional[str] = None, delegation_id: Optional[str] = None,
    parent_delegation_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "kind": kind, "id": id_, "title": title, "state": state,
        "privacy_tier": privacy_tier.value, "prompt": prompt, "model": model,
        "current_tool": current_tool, "tokens": tokens, "cost_usd": cost_usd,
        "lease": lease, "artifacts": [], "elapsed_s": max(0.0, elapsed_s),
        "started_at": started_at, "actor": actor,
        "delegation_id": delegation_id, "parent_delegation_id": parent_delegation_id,
        "children": [],
    }


def _task_node(storage, task, now: float, subtask_privacy: PrivacyTier) -> dict:
    active = storage.active_claims(task.id, now)
    primary = next((c for c in active if c.role.value == "primary_manager"), None) or (
        active[0] if active else None
    )
    lease = ({"holder": primary.manager_id, "expires_at": primary.expires_at, "fence": None}
              if primary else None)
    terminal = task.status in TERMINAL_TASK_STATUSES
    elapsed = (task.updated_at if terminal else now) - task.created_at
    node = _base_node(
        kind="task", id_=task.id, title=_redact_title(task.title, subtask_privacy),
        state=task.status.value, privacy_tier=subtask_privacy, lease=lease,
        elapsed_s=elapsed, started_at=task.created_at, actor=task.current_manager,
    )
    return node


def _session_node(storage, session, redact: Redactor, now: float,
                  claims_by_id: dict[str, Any], task_tier: PrivacyTier) -> dict:
    claim = claims_by_id.get(session.claim_id) if session.claim_id else None
    lease = ({"holder": claim.manager_id, "expires_at": claim.expires_at, "fence": None}
              if claim else None)
    command = session.launch_command or session.shell_command
    terminal_end = session.ended_at if session.ended_at is not None else now
    return _base_node(
        kind="session", id_=session.id,
        title=f"{session.manager_id} ({session.mode.value})",
        state=session.status.value, privacy_tier=task_tier,
        prompt=_redact_text(command, task_tier, redact), lease=lease,
        elapsed_s=terminal_end - session.started_at, started_at=session.started_at,
        actor=session.manager_id, delegation_id=session.delegation_id,
        parent_delegation_id=session.parent_delegation_id,
    )


def _subtask_node(storage, subtask, redact: Redactor, now: float,
                  current_tool: Optional[str], runs: list) -> dict:
    tier = _safe_tier(subtask.privacy_tier)
    terminal = subtask.status in _TERMINAL_SUBTASK
    elapsed = (subtask.updated_at if terminal else now) - subtask.created_at
    latest_run = runs[-1] if runs else None
    tokens, cost = _tokens_and_cost(latest_run.metrics if latest_run else {})
    node = _base_node(
        kind="subtask", id_=subtask.id, title=_redact_title(subtask.title, tier),
        state=subtask.status.value, privacy_tier=tier,
        prompt=_redact_text(subtask.instructions, tier, redact),
        model=latest_run.model if latest_run else None,
        current_tool=current_tool, tokens=tokens, cost_usd=cost,
        #: No claim/lease concept exists for a subtask in this deployment — the
        #: federated `WorkQueue`'s ticket lease/fence lives in a separate
        #: service (RouterService) with no `subtask_id` handle back to this
        #: one. Honest `None` rather than a guessed value (docs/EVENT_BUS.md §7).
        lease=None, elapsed_s=elapsed, started_at=subtask.created_at,
        actor=subtask.assigned_worker, delegation_id=subtask.delegation_id,
        parent_delegation_id=subtask.parent_delegation_id,
    )
    node["children"] = [_run_node(r, now) for r in runs]
    return node


def _run_node(run, now: float) -> dict:
    tokens, cost = _tokens_and_cost(run.metrics)
    end = run.finished_at if run.finished_at is not None else now
    return _base_node(
        kind="run", id_=run.id, title=f"{run.worker_id} ({run.harness})",
        state=run.status.value, privacy_tier=PrivacyTier.repo_sensitive,
        model=run.model, tokens=tokens, cost_usd=cost,
        elapsed_s=end - run.started_at, started_at=run.started_at, actor=run.worker_id,
    )


def _review_node(review, task_tier: PrivacyTier, now: float) -> dict:
    terminal = review.status in _TERMINAL_REVIEW
    end = review.updated_at if terminal else now
    return _base_node(
        kind="review", id_=review.id, title=f"review by {review.assigned_to}",
        state=review.status.value, privacy_tier=task_tier,
        elapsed_s=end - review.created_at, started_at=review.created_at,
        actor=review.assigned_to, delegation_id=review.delegation_id,
        parent_delegation_id=review.parent_delegation_id,
    )


def _latest_tool_by_subtask(storage, task_id: str) -> dict[str, str]:
    """One query (docs/EVENT_BUS.md §7 step 7): every `tool.authorized` row for
    the task, oldest-first; keeping the last write per `subtask_id` in a dict
    comprehension-equivalent loop is `O(rows)`, not `O(subtasks)` queries."""
    rows = storage.list_bus_events(task_id=task_id, types=["tool.authorized"], limit=500)
    latest: dict[str, str] = {}
    for row in rows:
        sid = row.get("subtask_id")
        if not sid:
            continue
        tool = (row.get("payload") or {}).get("tool")
        if tool:
            latest[sid] = tool
    return latest


def _task_subtree(storage, task, redact: Redactor, now: float) -> dict[str, Any]:
    sessions = storage.list_sessions(task_id=task.id, limit=100)
    subtasks = storage.list_subtasks(task.id)
    reviews = storage.list_reviews(task.id)
    claims = {c.id: c for c in storage.list_claims(task.id)}
    tool_by_subtask = _latest_tool_by_subtask(storage, task.id)
    subtask_tiers = [s.privacy_tier for s in subtasks]
    effective_tier = strictest(subtask_tiers) if subtask_tiers else PrivacyTier.public

    root = _task_node(storage, task, now, effective_tier)

    by_deleg: dict[str, dict] = {}
    ordered: list[dict] = []
    for session in sessions:
        node = _session_node(storage, session, redact, now, claims, effective_tier)
        ordered.append(node)
        if node["delegation_id"]:
            by_deleg[node["delegation_id"]] = node
    # One batched query for every run under this task (docs/EVENT_BUS.md §8:
    # O(rows) with existing indexes), never one `list_runs` per subtask.
    runs_by_subtask: dict[str, list] = {}
    for run in storage.list_runs_for_task(task.id):
        runs_by_subtask.setdefault(run.subtask_id, []).append(run)
    subtask_by_id: dict[str, dict] = {}
    for subtask in subtasks:
        runs = runs_by_subtask.get(subtask.id, [])
        node = _subtask_node(storage, subtask, redact, now,
                             tool_by_subtask.get(subtask.id), runs)
        ordered.append(node)
        subtask_by_id[subtask.id] = node
        if node["delegation_id"]:
            by_deleg[node["delegation_id"]] = node
    for review in reviews:
        node = _review_node(review, effective_tier, now)
        ordered.append(node)
        if node["delegation_id"]:
            by_deleg[node["delegation_id"]] = node

    for node in ordered:
        parent_id = node["parent_delegation_id"]
        parent = by_deleg.get(parent_id) if parent_id else None
        (parent["children"] if parent is not None else root["children"]).append(node)

    for artifact in storage.list_artifacts(task.id):
        target = subtask_by_id.get((artifact.metadata or {}).get("subtask_id"))
        (target["artifacts"] if target is not None else root["artifacts"]).append(
            _artifact_node(artifact)
        )
    return root


#: Bounded scan for the all-tasks case — a fleet-wide operator view, not a
#: paginated listing surface; large enough that a realistic deployment's open
#: work fits in one call.
_MAX_ROOT_TASKS = 500


def build_observe_tree(
    storage, *, redact: Redactor, task_id: Optional[str] = None,
    include_terminal: bool = False, now: Optional[float] = None,
) -> dict[str, Any]:
    at = now if now is not None else time.time()
    if task_id:
        task = storage.get_task(task_id)
        tasks = [task] if task is not None else []
    else:
        # One SELECT materializing full rows — never a lossy summary listing
        # followed by a `get_task` per survivor (the N+1 the first
        # implementation had: every column was already read once).
        tasks = [
            t for t in storage.list_tasks_full(TaskFilters(limit=_MAX_ROOT_TASKS))
            if include_terminal or t.status not in TERMINAL_TASK_STATUSES
        ]
    roots = [_task_subtree(storage, t, redact, at) for t in tasks]
    return {"generated_at": at, "latest_seq": storage.latest_bus_seq(), "roots": roots}
