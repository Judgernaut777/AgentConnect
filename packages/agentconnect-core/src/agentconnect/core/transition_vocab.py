"""Engine-A vocabulary registry: the concrete :class:`Vocabulary` instances
and cross-vocabulary mapping tables for every state-carrying table
`AgentConnectService` owns (spec: one transition authority, goal item 3).

This is the ONLY place any of these edge tables are declared as data; the
enums themselves stay in :mod:`agentconnect.core.models` /
:mod:`agentconnect.core.execution`, and `agentconnect.core.subtasks` /
`agentconnect.core.reviews` keep owning their own `TRANSITIONS` dicts (reused
here verbatim, not duplicated) since other code still imports those directly
for `check_transition`.

Deliberately imports `agentconnect.common.transitions` (the generic engine)
plus `agentconnect.core.*` (the concrete enums) — this is the one place in the
tree those two meet. `agentconnect.common.transitions` itself imports neither
`core` nor `router`, so this file is the only new edge, and it points the
direction the packaging already ships (`common` and `core` are one wheel,
`agentconnect-core`; nothing outside this file needs to know the split).
"""

from __future__ import annotations

from ..common.transitions import Vocabulary
from . import reviews as reviews_policy
from . import subtasks as subtasks_policy
from .execution import ExecutionState
from .models import (
    ApprovalStatus,
    ReviewStatus,
    RunStatus,
    SessionStatus,
    SubtaskStatus,
    TaskStatus,
)

# --------------------------------------------------------------- task_status
#: Reconstructed from every real write site in `AgentConnectService` (verified
#: by grep against `_advance_task`/`_touch(...status=...)`/`claim_task`'s raw
#: UPDATE): `_TO_IN_PROGRESS`/`_TO_NEEDS_REVIEW`/`_TO_NEEDS_APPROVAL` plus
#: `complete_review`'s direct needs_review->in_progress,
#: `_settle_parent_after_subtask`'s direct needs_approval->in_progress, and
#: `claim_task`'s queued->in_progress. `succeeded` is (today) reachable
#: unconditionally from `complete_task` regardless of current status except
#: an explicit already-succeeded guard — migrating this through the strict
#: `transition()` verb closes the latent "complete a cancelled/failed task"
#: gap (spec: "Approval expiry"/"claim_task composition" adjudication,
#: complete_task item). `failed` has NO writer today (no code ever sets
#: `TaskStatus.failed`); kept terminal with empty in-edges so a future writer
#: is not accidentally unguarded. `blocked` likewise has no writer; its edges
#: mirror `queued` so it is not stranded if one is added later.
_TASK_STATUS_EDGES: dict[TaskStatus, frozenset] = {
    TaskStatus.queued: frozenset({
        TaskStatus.in_progress, TaskStatus.needs_review, TaskStatus.needs_approval,
        TaskStatus.succeeded,
    }),
    TaskStatus.in_progress: frozenset({
        TaskStatus.needs_review, TaskStatus.needs_approval, TaskStatus.succeeded,
    }),
    TaskStatus.needs_review: frozenset({TaskStatus.in_progress, TaskStatus.succeeded}),
    TaskStatus.needs_approval: frozenset({TaskStatus.in_progress, TaskStatus.succeeded}),
    TaskStatus.blocked: frozenset({
        TaskStatus.in_progress, TaskStatus.needs_review, TaskStatus.needs_approval,
        TaskStatus.succeeded,
    }),
    TaskStatus.failed: frozenset(),
    TaskStatus.succeeded: frozenset(),
    TaskStatus.cancelled: frozenset(),
}

TASK_STATUS_VOCAB: Vocabulary[TaskStatus] = Vocabulary(
    name="task_status",
    enum_type=TaskStatus,
    column="status",
    edges=_TASK_STATUS_EDGES,
    terminal=frozenset({TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled}),
    universal=frozenset({TaskStatus.cancelled}),
)

# ------------------------------------------------------------ subtask_status
SUBTASK_STATUS_VOCAB: Vocabulary[SubtaskStatus] = Vocabulary(
    name="subtask_status",
    enum_type=SubtaskStatus,
    column="status",
    edges=dict(subtasks_policy.TRANSITIONS),
    terminal=frozenset(subtasks_policy.TERMINAL),
)

# ----------------------------------------------------------------- run_status
_RUN_STATUS_EDGES: dict[RunStatus, frozenset] = {
    RunStatus.running: frozenset({RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}),
    RunStatus.succeeded: frozenset(),
    RunStatus.failed: frozenset(),
    RunStatus.cancelled: frozenset(),
}

RUN_STATUS_VOCAB: Vocabulary[RunStatus] = Vocabulary(
    name="run_status",
    enum_type=RunStatus,
    column="status",
    edges=_RUN_STATUS_EDGES,
    terminal=frozenset({RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}),
)

# -------------------------------------------------------------- review_status
REVIEW_STATUS_VOCAB: Vocabulary[ReviewStatus] = Vocabulary(
    name="review_status",
    enum_type=ReviewStatus,
    column="status",
    edges=dict(reviews_policy.TRANSITIONS),
    terminal=frozenset(reviews_policy.TERMINAL),
)

# ------------------------------------------------------------ approval_status
_APPROVAL_STATUS_EDGES: dict[ApprovalStatus, frozenset] = {
    ApprovalStatus.pending: frozenset({
        ApprovalStatus.granted, ApprovalStatus.denied, ApprovalStatus.expired,
    }),
    ApprovalStatus.granted: frozenset(),
    ApprovalStatus.denied: frozenset(),
    ApprovalStatus.expired: frozenset(),
}

APPROVAL_STATUS_VOCAB: Vocabulary[ApprovalStatus] = Vocabulary(
    name="approval_status",
    enum_type=ApprovalStatus,
    column="status",
    edges=_APPROVAL_STATUS_EDGES,
    terminal=frozenset({ApprovalStatus.granted, ApprovalStatus.denied, ApprovalStatus.expired}),
)

# ------------------------------------------------------------- session_status
#: `prepared` reaches every terminal directly too, not just `running`: a
#: launched session can be ended/failed via `end_shell` (or swept abandoned)
#: without a shell ever having been started (verified: `end_shell` has no
#: precondition on status in the pre-authority code — it unconditionally
#: wrote ended/failed regardless of current status).
_SESSION_STATUS_EDGES: dict[SessionStatus, frozenset] = {
    SessionStatus.prepared: frozenset({
        SessionStatus.running, SessionStatus.ended, SessionStatus.failed,
        SessionStatus.abandoned,
    }),
    SessionStatus.running: frozenset({
        SessionStatus.ended, SessionStatus.failed, SessionStatus.abandoned,
    }),
    SessionStatus.ended: frozenset(),
    SessionStatus.failed: frozenset(),
    SessionStatus.abandoned: frozenset(),
}

SESSION_STATUS_VOCAB: Vocabulary[SessionStatus] = Vocabulary(
    name="session_status",
    enum_type=SessionStatus,
    column="status",
    edges=_SESSION_STATUS_EDGES,
    terminal=frozenset({SessionStatus.ended, SessionStatus.failed, SessionStatus.abandoned}),
)

# ------------------------------------------------------------ execution_state
#: A deliberately PERMISSIVE mirror table, not a strict pipeline FSM (spec:
#: "no FSM for ExecutionState stricter than the permissive mirror"). `unknown`
#: (a handle that predates observability, or one probed before any state was
#: ever recorded) can move to anything; the three live states are fully
#: interconnected (approval <-> review <-> running is a real oscillation: a
#: denied approval can re-route into another approval wait, etc.) and each can
#: terminate.
_EXECUTION_LIVE = frozenset({
    ExecutionState.running, ExecutionState.waiting_approval, ExecutionState.waiting_review,
})
_EXECUTION_TERMINAL = frozenset({
    ExecutionState.completed, ExecutionState.failed, ExecutionState.cancelled,
})
_EXECUTION_STATE_EDGES: dict[ExecutionState, frozenset] = {
    ExecutionState.unknown: frozenset(_EXECUTION_LIVE | _EXECUTION_TERMINAL),
    **{
        live: frozenset((_EXECUTION_LIVE - {live}) | _EXECUTION_TERMINAL)
        for live in _EXECUTION_LIVE
    },
    ExecutionState.completed: frozenset(),
    ExecutionState.failed: frozenset(),
    ExecutionState.cancelled: frozenset(),
}

EXECUTION_STATE_VOCAB: Vocabulary[ExecutionState] = Vocabulary(
    name="execution_state",
    enum_type=ExecutionState,
    column="state",
    edges=_EXECUTION_STATE_EDGES,
    terminal=_EXECUTION_TERMINAL,
)

# ------------------------------------------------------------ mapping tables
#: (b) LIVE, same-engine: subtask <-> run pairing (formalizes the pairing
#: `_record_result` / `cancel_subtask` already assume in comments). Tested for
#: totality + exercised end-to-end in `test_transition_vocabulary_mapping.py`.
SUBTASK_RUN_PAIRING: dict[SubtaskStatus, RunStatus] = {
    SubtaskStatus.running: RunStatus.running,
    SubtaskStatus.succeeded: RunStatus.succeeded,
    SubtaskStatus.failed: RunStatus.failed,
    SubtaskStatus.cancelled: RunStatus.cancelled,
}

#: (c) DOCUMENTARY Rosetta stone: Engine A (`TaskStatus`) <-> Engine B
#: (`TaskState`, common.schemas) never share a row (disjoint id keyspaces —
#: `task_*` ids vs router `task_*` ids from a different minting sequence and
#: store). This exists for humans/audit tooling reading both ledgers side by
#: side; it is tested only for totality, never enforced at a write site.
#: `REJECTED` has two outgoing FSM edges (COMPLETE, RETRY) — mapped to
#: `needs_review` as the closest human meaning (a rejection is something a
#: human still needs to look at). `blocked`/`needs_approval` have no
#: `TaskState` equivalent (Engine-A-only concepts); they are not keys here.
def _rosetta() -> dict:
    from ..common.schemas import TaskState

    return {
        TaskState.CREATED: TaskStatus.queued,
        TaskState.CLASSIFIED: TaskStatus.queued,
        TaskState.PRIVACY_CHECKED: TaskStatus.queued,
        TaskState.ELIGIBLE_PROVIDERS_COMPUTED: TaskStatus.queued,
        TaskState.QUEUED: TaskStatus.queued,
        TaskState.DISPATCHED: TaskStatus.in_progress,
        TaskState.RUNNING: TaskStatus.in_progress,
        TaskState.ARTIFACTS_WRITTEN: TaskStatus.in_progress,
        TaskState.CHECKS_RUN: TaskStatus.in_progress,
        TaskState.APPROVED: TaskStatus.in_progress,
        TaskState.RETRY: TaskStatus.in_progress,
        TaskState.REVIEW_READY: TaskStatus.needs_review,
        TaskState.REJECTED: TaskStatus.needs_review,
        TaskState.COMPLETE: TaskStatus.succeeded,
        TaskState.CANCELLED: TaskStatus.cancelled,
        TaskState.FAILED: TaskStatus.failed,
    }


TASK_ROSETTA: dict = _rosetta()

__all__ = [
    "TASK_STATUS_VOCAB",
    "SUBTASK_STATUS_VOCAB",
    "RUN_STATUS_VOCAB",
    "REVIEW_STATUS_VOCAB",
    "APPROVAL_STATUS_VOCAB",
    "SESSION_STATUS_VOCAB",
    "EXECUTION_STATE_VOCAB",
    "SUBTASK_RUN_PAIRING",
    "TASK_ROSETTA",
]
