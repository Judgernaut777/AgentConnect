"""One transition authority (docs/CONSISTENCY_REVIEW.md consolidation pass).

This module owns the shared machinery every state-carrying table in the
ecosystem drives its writes through: FSM edges as data (:class:`Vocabulary`),
a terminal-guarded, fresh-read compare-and-set write pattern, an idempotent
cancel semantic, and a mandatory audit-record hook.

Deliberately dependency-light: pure stdlib, and it imports nothing from
``agentconnect.core`` or ``agentconnect.router`` (nor from ``common.memory`` /
``common.workqueue``) so it can sit underneath every engine without creating a
new dependency edge. A caller supplies a :class:`Vocabulary` (the FSM, as
data) and a ``writer`` callable (a :class:`LockedWriter`) that knows how to
read-validate-write a specific backend table under that backend's own lock;
this module supplies the verbs (`transition`, `advance`, `converge`,
`cancel`) that build the read-validate-write decision and interpret the
outcome.

Every verb re-reads the stored value *inside* the writer's lock span and
validates against that fresh read — never against a caller-supplied `current`
— because a caller-supplied value can be stale (read outside the lock) and an
exact-match guard against it produces spurious refusals for legal concurrent
writes. The only thing a caller-supplied ``current`` is good for is a nicer
error message, and this module never asks for one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Mapping, Optional, Protocol, TypeVar

E = TypeVar("E", bound=Enum)

__all__ = [
    "IllegalTransition",
    "TransitionRefused",
    "EntityNotFound",
    "Vocabulary",
    "TransitionRecord",
    "LockedWriter",
    "TransitionAuthority",
    "CancelResult",
]


class IllegalTransition(ValueError):
    """``dst`` is not a legal edge from the freshly-read stored state, and the
    stored state is NOT terminal (a terminal source is a :class:`TransitionRefused`,
    not this — a terminal-is-final refusal is an expected outcome of concurrent
    cancellation, not a caller bug). This is raised for a genuinely illegal edge:
    the caller asked for a transition the FSM never allows."""

    def __init__(self, vocabulary: str, entity_id: str, src: str, dst: str):
        super().__init__(f"{vocabulary}: illegal {entity_id}: {src} -> {dst}")
        self.vocabulary = vocabulary
        self.entity_id = entity_id
        self.src = src
        self.dst = dst


class TransitionRefused(RuntimeError):
    """The freshly-read stored state is TERMINAL (or the entity vanished
    cross-process between two reads) and ``dst`` is not reachable from it.
    Carries the stored value — the generalization of the router's
    ``TaskSuperseded``."""

    def __init__(self, entity_id: str, stored: Optional[str]):
        self.entity_id = entity_id
        self.stored = stored
        super().__init__(f"{entity_id}: transition refused, stored={stored!r}")


class EntityNotFound(LookupError):
    def __init__(self, vocabulary: str, entity_id: str):
        super().__init__(f"unknown {vocabulary} entity {entity_id!r}")
        self.vocabulary = vocabulary
        self.entity_id = entity_id


@dataclass(frozen=True)
class Vocabulary(Generic[E]):
    """One FSM, as data. ``column`` is the real database column name the
    authority writes ``dst.value`` into — the mechanism, not documentation —
    so every backend writer for this vocabulary must use the same column
    name for its state/status field."""

    name: str
    enum_type: type
    column: str
    edges: Mapping[E, frozenset]
    terminal: frozenset
    #: Reachable from any NON-terminal member regardless of ``edges`` (e.g.
    #: CANCELLED). Never added on top of a terminal source.
    universal: frozenset = field(default_factory=frozenset)

    def allowed(self, src: E, overlay: Optional[Mapping[E, frozenset]] = None) -> frozenset:
        base = set(self.edges.get(src, frozenset()))
        if src not in self.terminal:
            base |= set(self.universal)
            if overlay:
                base |= set(overlay.get(src, frozenset()))
        return frozenset(base)

    def is_terminal(self, s: E) -> bool:
        return s in self.terminal


@dataclass(frozen=True)
class TransitionRecord:
    """The mandatory audit payload. Built by the authority's verbs, never by
    callers directly. ``message`` is an optional precomputed human-readable
    line for a backend whose audit sink needs to preserve an exact legacy
    string (e.g. WorkQueue's task-state mirror log lines); when absent the
    writer falls back to a generic rendering."""

    vocabulary: str
    entity_id: str
    src: str
    dst: str
    outcome: str  # "applied" | "refused" | "noop"
    actor: str
    task_id: Optional[str] = None
    reason: str = ""
    message: Optional[str] = None


#: `decide(current_raw) -> (fields_or_None, record_or_None)`. `current_raw` is
#: the FRESH, lock-held read of the stored column value (a plain str, or None
#: if the entity row does not exist). Returning `(fields, record)` with
#: `fields` non-None means "proceed": the writer sets those columns (which
#: MUST include `{vocabulary.column: dst.value}`) via one guarded
#: `UPDATE ... WHERE id=? AND column=<current_raw>` and, if `record` is not
#: None, inserts it as an audit row on the SAME connection before the single
#: commit that covers both. Returning `(None, record)` means "do not write";
#: if `record` is not None it is still audited (a refusal is itself an
#: auditable event). Returning `(None, None)` means "do not write, and do not
#: audit" — the silent, already-converged no-op a mirror replay produces.
DecideFn = Callable[[Optional[str]], "tuple[Optional[dict], Optional[TransitionRecord]]"]


class LockedWriter(Protocol):
    """One per (backend, table). MUST, inside ONE lock-held span:

    1. SELECT the current column value fresh (``None`` if the row is missing).
    2. Call ``decide(current)``.
    3. If it returned fields: ``UPDATE ... WHERE id=? AND column=<the fresh
       current>`` (single statement — exact-match CAS against the value just
       read, belt-and-braces against a CROSS-PROCESS writer only; every
       in-process writer already serializes on one lock, so an in-lock read is
       race-free in-process).
    4. If it returned a record (whether writing or not): INSERT the audit row
       on the SAME connection — never a self-committing helper.
    5. Commit once for both (or, given a composed ``conn``, do not commit —
       the caller's outer transaction commits everything together).
    6. On a CAS miss (rowcount 0, cross-process only), retry the whole
       read-decide-write span up to a small bounded number of times before
       giving up and reporting the latest observed value.

    Returns ``(applied, stored_value_after)``: ``stored_value_after`` is the
    pre-existing value on refusal/noop, the new value on success, and
    ``None`` if the entity does not exist.
    """

    def __call__(
        self, entity_id: str, decide: DecideFn, conn: Any = None,
    ) -> "tuple[bool, Optional[str]]": ...


@dataclass
class CancelResult(Generic[E]):
    entity_id: str
    applied: bool
    state: E
    already_terminal: bool
    #: The freshly-read state immediately BEFORE this call (== `state` when
    #: `already_terminal`; the pre-cancel live state when `applied`). For
    #: audit/log messages that want to say "was X" without a second, possibly
    #: stale, re-read after the fact.
    previous_state: Optional[E] = None


def _default_message(record: TransitionRecord) -> str:
    if record.message is not None:
        return record.message
    if record.outcome == "applied":
        return f"{record.vocabulary} {record.entity_id}: {record.src} -> {record.dst} ({record.actor})"
    return (
        f"{record.vocabulary} {record.entity_id}: refused {record.src} -> {record.dst}"
        f" ({record.reason})"
    )


@dataclass
class TransitionAuthority(Generic[E]):
    vocabulary: "Vocabulary[E]"
    writer: LockedWriter

    # ------------------------------------------------------------- transition
    def transition(
        self,
        entity_id: str,
        dst: E,
        *,
        actor: str = "system",
        task_id: Optional[str] = None,
        extra_fields: Optional[dict] = None,
        conn: Any = None,
    ) -> E:
        """Strict verb: raises :class:`EntityNotFound` for an unknown entity,
        :class:`IllegalTransition` for a genuinely illegal edge from a
        non-terminal stored state (a caller bug — loud by design), and
        :class:`TransitionRefused` when the freshly-read stored state is
        terminal. Returns the new (or, if already there, the unchanged)
        state on success."""
        vocab = self.vocabulary

        def decide(current_raw):
            if current_raw is None:
                return None, None
            current = vocab.enum_type(current_raw)
            if current == dst:
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, dst.value, "noop", actor,
                    task_id, "already at destination",
                )
                return None, record
            if vocab.is_terminal(current):
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, dst.value, "refused", actor,
                    task_id, "terminal state is final",
                )
                return None, record
            if dst not in vocab.allowed(current):
                raise IllegalTransition(vocab.name, entity_id, current.value, dst.value)
            fields = dict(extra_fields or {})
            fields[vocab.column] = dst.value
            record = TransitionRecord(
                vocab.name, entity_id, current.value, dst.value, "applied", actor, task_id,
            )
            return fields, record

        applied, stored = self.writer(entity_id, decide, conn)
        if stored is None:
            raise EntityNotFound(vocab.name, entity_id)
        current_enum = vocab.enum_type(stored)
        if applied or current_enum == dst:
            return current_enum
        raise TransitionRefused(entity_id, stored)

    # ----------------------------------------------------------------- advance
    def advance(
        self,
        entity_id: str,
        dst: E,
        *,
        only_from: frozenset,
        actor: str = "system",
        task_id: Optional[str] = None,
        extra_fields: Optional[dict] = None,
        conn: Any = None,
    ) -> Optional[E]:
        """Soft verb (the service's ``_advance_task`` semantic, made atomic):
        inside the locked span, a no-op (returns ``None``, audits ``"noop"``)
        when the fresh current is terminal, already ``dst``, or not in
        ``only_from``; otherwise transitions and returns ``dst``. Never
        raises for those three cases — this is the "maybe" verb, used for
        soft status hints a slower concurrent actor must not clobber."""
        vocab = self.vocabulary

        def decide(current_raw):
            if current_raw is None:
                return None, None
            current = vocab.enum_type(current_raw)
            if current == dst or vocab.is_terminal(current) or current not in only_from:
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, dst.value, "noop", actor, task_id,
                    "not eligible for this advance",
                )
                return None, record
            fields = dict(extra_fields or {})
            fields[vocab.column] = dst.value
            record = TransitionRecord(
                vocab.name, entity_id, current.value, dst.value, "applied", actor, task_id,
            )
            return fields, record

        applied, _stored = self.writer(entity_id, decide, conn)
        # `None` means "this call did not perform the write" for ANY reason —
        # entity missing, already terminal, already at `dst`, or not in
        # `only_from` — the single falsy signal callers use to detect "I lost
        # the race" (mirrors the old boolean `update_subtask_if_status`
        # contract). Distinguishing *why* is what the audit record is for, not
        # the return value.
        return dst if applied else None

    # ---------------------------------------------------------------- converge
    def converge(
        self,
        entity_id: str,
        dst: E,
        *,
        actor: str = "system",
        overlay: Optional[Mapping[E, frozenset]] = None,
        task_id: Optional[str] = None,
        extra_fields: Optional[dict] = None,
        on_applied_message: Optional[Callable[[E, E], str]] = None,
        on_refused_message: Optional[Callable[[E, E], str]] = None,
        conn: Any = None,
    ) -> "tuple[bool, Optional[E]]":
        """Mirror verb (a ticket-status/task-state or execution-handle
        mirror re-driving a linked entity's status). Fresh current == dst ->
        silent no-op success (no write, no audit row — matches today's
        silent self-heal replay). Current terminal and != dst -> refused,
        audited. Missing entity -> ``(False, None)``, nothing to do. Else a
        transition via ``overlay``-augmented edges."""
        vocab = self.vocabulary

        def decide(current_raw):
            if current_raw is None:
                return None, None
            current = vocab.enum_type(current_raw)
            if current == dst:
                return None, None
            if vocab.is_terminal(current):
                msg = on_refused_message(current, dst) if on_refused_message else None
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, dst.value, "refused", actor, task_id,
                    "terminal state is final", msg,
                )
                return None, record
            if dst not in vocab.allowed(current, overlay=overlay):
                msg = on_refused_message(current, dst) if on_refused_message else None
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, dst.value, "refused", actor, task_id,
                    "illegal edge", msg,
                )
                return None, record
            msg = on_applied_message(current, dst) if on_applied_message else None
            fields = dict(extra_fields or {})
            fields[vocab.column] = dst.value
            record = TransitionRecord(
                vocab.name, entity_id, current.value, dst.value, "applied", actor, task_id,
                message=msg,
            )
            return fields, record

        applied, stored = self.writer(entity_id, decide, conn)
        if stored is None:
            return False, None
        current_enum = vocab.enum_type(stored)
        return applied, (dst if applied else current_enum)

    # ------------------------------------------------------------------ cancel
    def cancel(
        self,
        entity_id: str,
        *,
        cancelled: E,
        actor: str = "system",
        task_id: Optional[str] = None,
        extra_fields: Optional[dict] = None,
        conn: Any = None,
    ) -> CancelResult[E]:
        """THE converged cancel semantic. A fresh current that is already
        terminal (pre-read OR raced against a concurrent terminal writer) is
        ALWAYS a no-op SUCCESS reporting the stored state — never an
        exception, never error-shaped. An unknown entity still raises
        :class:`EntityNotFound` — that is a different failure class (the
        caller asked about something that never existed, not something
        already finished)."""
        vocab = self.vocabulary
        seen: list = []

        def decide(current_raw):
            if current_raw is None:
                return None, None
            current = vocab.enum_type(current_raw)
            seen.append(current)
            if current == cancelled:
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, cancelled.value, "noop", actor,
                    task_id, "already cancelled",
                )
                return None, record
            if vocab.is_terminal(current):
                record = TransitionRecord(
                    vocab.name, entity_id, current.value, cancelled.value, "noop", actor,
                    task_id, "already terminal",
                )
                return None, record
            fields = dict(extra_fields or {})
            fields[vocab.column] = cancelled.value
            record = TransitionRecord(
                vocab.name, entity_id, current.value, cancelled.value, "applied", actor, task_id,
            )
            return fields, record

        applied, stored = self.writer(entity_id, decide, conn)
        if stored is None:
            raise EntityNotFound(vocab.name, entity_id)
        current_enum = vocab.enum_type(stored)
        previous = seen[-1] if seen else current_enum
        if applied:
            return CancelResult(entity_id, True, cancelled, False, previous_state=previous)
        return CancelResult(entity_id, False, current_enum, True, previous_state=previous)
