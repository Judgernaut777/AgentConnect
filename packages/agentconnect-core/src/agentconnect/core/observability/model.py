"""The provider-neutral observation event model (production handoff Part III).

AgentConnect owns this vocabulary. A lifecycle change in `AgentConnectService`
is translated into exactly one :class:`AgentObservationEvent`, and that event is
handed to zero or more configured providers. No provider ever sees an
AgentConnect model object; it sees this normalized shape. That is what lets a
tmux pane, a JSONL line, and an OTLP span all carry the *same* correlation ids.

Two rules the model enforces by construction:

* **No hidden chain-of-thought.** There is no field for a prompt, a completion,
  or an agent's reasoning. `metadata` is for provider-native *state names* and
  small scalars, never transcripts. Sensitive capture is opt-in and travels a
  different, redacted path — never this event.
* **Correlation without timestamp guessing.** Every event carries the full id
  set (`trace_id`, `task_id`, `delegation_id`, `parent_delegation_id`,
  `subtask_id`, `session_id`, `run_id`, `review_id`), so a tree or a trace is
  reconstructable from the events alone.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> float:
    return time.time()


class EventType(str, Enum):
    """Every lifecycle transition AgentConnect makes observable (Part III).

    The string values are stable wire identifiers: a JSONL reader, a CLI filter,
    and an OTLP attribute all match on them, so they must not change once shipped.
    """

    # Task
    task_created = "task.created"
    task_claimed = "task.claimed"
    task_released = "task.released"
    task_completed = "task.completed"
    task_failed = "task.failed"
    task_cancelled = "task.cancelled"

    # Session (a launched, managed agent)
    session_prepared = "session.prepared"
    session_started = "session.started"
    session_ended = "session.ended"

    # Subtask (a delegated unit of work)
    subtask_created = "subtask.created"
    subtask_routed = "subtask.routed"
    subtask_approved = "subtask.approved"
    subtask_denied = "subtask.denied"

    # Worker run
    worker_spawned = "worker.spawned"
    worker_started = "worker.started"
    worker_completed = "worker.completed"
    worker_failed = "worker.failed"

    # Review
    review_requested = "review.requested"
    review_spawned = "review.spawned"
    review_claimed = "review.claimed"
    review_completed = "review.completed"

    # Ledger records
    artifact_created = "artifact.created"
    attempt_recorded = "attempt.recorded"
    decision_recorded = "decision.recorded"

    # Governance / cross-plane
    tool_authorized = "tool.authorized"
    compute_placed = "compute.placed"
    memory_recalled = "memory.recalled"
    memory_captured = "memory.captured"

    # Audit
    audit_started = "audit.started"
    audit_passed = "audit.passed"
    audit_failed = "audit.failed"

    # Reconciliation (an orphaned session/run whose process died with no
    # terminal event, swept to a terminal state by the reconcile pass).
    session_reconciled = "session.reconciled"
    run_reconciled = "run.reconciled"


class ObservationState(str, Enum):
    """The normalized agent state model (Part III).

    A provider's own state name (tmux has none; Herdr has its own; a scheduler
    has more) is preserved verbatim in ``metadata['provider_state']``. This enum
    is the *common* denominator every surface agrees on, so `agents list` reads
    the same whichever provider is installed.
    """

    prepared = "prepared"
    starting = "starting"
    working = "working"
    waiting = "waiting"
    blocked = "blocked"
    idle = "idle"
    retrying = "retrying"
    reviewing = "reviewing"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"


class ObservationOutcome(str, Enum):
    """How a unit of observed work ended. Absent while it is still in flight."""

    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    denied = "denied"
    timed_out = "timed_out"
    unknown = "unknown"


#: Which normalized state each event type implies when the caller does not name
#: one. Keeps emission sites terse: most call `observe(event_type=...)` and the
#: state follows.
DEFAULT_STATE_FOR_EVENT: dict[EventType, ObservationState] = {
    EventType.task_created: ObservationState.prepared,
    EventType.task_claimed: ObservationState.working,
    EventType.task_released: ObservationState.idle,
    EventType.task_completed: ObservationState.done,
    EventType.task_failed: ObservationState.failed,
    EventType.task_cancelled: ObservationState.cancelled,
    EventType.session_prepared: ObservationState.prepared,
    EventType.session_started: ObservationState.working,
    EventType.session_ended: ObservationState.done,
    EventType.subtask_created: ObservationState.prepared,
    EventType.subtask_routed: ObservationState.starting,
    EventType.subtask_approved: ObservationState.working,
    EventType.subtask_denied: ObservationState.blocked,
    EventType.worker_spawned: ObservationState.starting,
    EventType.worker_started: ObservationState.working,
    EventType.worker_completed: ObservationState.done,
    EventType.worker_failed: ObservationState.failed,
    EventType.review_requested: ObservationState.waiting,
    EventType.review_spawned: ObservationState.reviewing,
    EventType.review_claimed: ObservationState.reviewing,
    EventType.review_completed: ObservationState.done,
    EventType.artifact_created: ObservationState.working,
    EventType.attempt_recorded: ObservationState.working,
    EventType.decision_recorded: ObservationState.working,
    EventType.tool_authorized: ObservationState.working,
    EventType.compute_placed: ObservationState.starting,
    EventType.memory_recalled: ObservationState.working,
    EventType.memory_captured: ObservationState.working,
    EventType.audit_started: ObservationState.reviewing,
    EventType.audit_passed: ObservationState.done,
    EventType.audit_failed: ObservationState.failed,
    EventType.session_reconciled: ObservationState.failed,
    EventType.run_reconciled: ObservationState.failed,
}


class AgentObservationEvent(BaseModel):
    """One normalized, provider-neutral observation (Part III).

    ``event_id`` is the idempotency key: an emitter (or a provider) that has seen
    an id must treat a second arrival as a duplicate. ``sequence`` is a monotonic
    per-trace counter that lets a reader restore order even when events arrive
    out of order (a JSONL tail, a batched OTLP export, a replayed workflow).
    """

    event_id: str
    sequence: int = 0
    timestamp: float = Field(default_factory=_now)

    event_type: EventType
    state: ObservationState = ObservationState.unknown
    outcome: Optional[ObservationOutcome] = None

    # Correlation — the full id set, so nothing has to be inferred from time.
    trace_id: str
    task_id: Optional[str] = None
    delegation_id: Optional[str] = None
    parent_delegation_id: Optional[str] = None
    subtask_id: Optional[str] = None
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    review_id: Optional[str] = None

    # Identity of the observed agent.
    agent_id: str = "unknown"
    agent_role: str = "unknown"

    # Where it lives.
    provider: str = ""
    workspace_id: Optional[str] = None

    #: Provider-native state and small scalars only. NEVER prompts, completions,
    #: or reasoning. The safety layer redacts anything routed here on the opt-in
    #: sensitive-capture path; by default this holds ids and enum strings.
    metadata: dict[str, Any] = Field(default_factory=dict)

    def dedupe_key(self) -> tuple[str, int]:
        """`(trace_id, sequence)` — the alternative dedupe identity from Part IV.

        A provider may dedupe on ``event_id`` (exact) or on this pair (positional).
        Both are stable across a replay that re-emits the same transition.
        """
        return (self.trace_id, self.sequence)


class ProviderHealth(BaseModel):
    """A provider's self-report. ``available`` gates whether the composite fans
    an event out to it; ``detail`` is for a human reading `observability health`."""

    provider: str
    available: bool = True
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationHandle(BaseModel):
    """An opaque, durable pointer a provider hands back after it starts observing
    a session or a process. AgentConnect stores it and passes it back to
    ``attach_info`` / ``close``; only the issuing provider interprets ``target``.

    For tmux, ``target`` is ``session:window.pane``. For a JSONL provider it is
    the delegation id. For Herdr it would be the workspace/tab/pane triple.
    """

    provider: str
    handle_id: str
    kind: str = "session"  # session | process
    target: str = ""
    delegation_id: Optional[str] = None
    trace_id: Optional[str] = None
    task_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AttachInformation(BaseModel):
    """How a human reaches a live agent. ``attach_command`` is exact and runnable;
    ``read_only_command`` attaches without being able to type into the pane."""

    provider: str
    available: bool = False
    attach_command: str = ""
    read_only_command: str = ""
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapturedOutput(BaseModel):
    """Bounded, redacted terminal output. ``truncated`` says the capture hit its
    line bound; ``redacted`` says the safety layer replaced at least one span."""

    provider: str
    handle_id: str
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False
    redacted: bool = False
    detail: str = ""


# ---------------------------------------------------------------- requests
class SessionObservationRequest(BaseModel):
    """Ask a provider to begin observing a managed agent *session* (a manager or
    reviewer shell). The provider creates whatever live surface it offers — a
    tmux pane, a Herdr tab — and returns a handle."""

    trace_id: str
    task_id: Optional[str] = None
    session_id: str
    delegation_id: Optional[str] = None
    parent_delegation_id: Optional[str] = None
    review_id: Optional[str] = None
    agent_id: str = "unknown"
    agent_role: str = "manager"
    workspace_id: Optional[str] = None
    workspace_path: Optional[str] = None
    #: What a live provider should run in the pane. Empty means an idle shell;
    #: a provider that cannot run a command ignores it.
    command: str = ""
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpawnObservationRequest(BaseModel):
    """Ask a provider to begin observing a spawned *process* (a worker run). Same
    shape as a session request plus the run id and the delegated subtask."""

    trace_id: str
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    subtask_id: Optional[str] = None
    run_id: Optional[str] = None
    delegation_id: Optional[str] = None
    parent_delegation_id: Optional[str] = None
    agent_id: str = "unknown"
    agent_role: str = "worker"
    workspace_id: Optional[str] = None
    workspace_path: Optional[str] = None
    command: str = ""
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class StateObservationRequest(BaseModel):
    """Tell a provider an observed unit changed state, without a full event. A
    live provider updates its pane title/border; a log provider records a line."""

    handle: ObservationHandle
    state: ObservationState
    provider_state: Optional[str] = None
    outcome: Optional[ObservationOutcome] = None
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
