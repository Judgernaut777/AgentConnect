"""SQLite persistence for the task ledger (spec §8).

The *class* is the storage seam: `AgentConnectService` only ever calls the method
surface below, never SQL. A Postgres implementation later needs to provide the
same methods plus `transaction()` with the same semantics — a serialized
read-modify-write span, so invariants like "one active primary_manager claim"
hold under concurrency.

Concurrency note (learned the hard way in `common/memory.py`): a SQLite
transaction belongs to the *connection*, not the thread. A FastAPI sync endpoint
pool shares one connection across threads, so a peer's `commit()` would land in
the middle of another thread's read-modify-write. Every write path therefore runs
under one reentrant lock held across the whole execute→commit span, and file DBs
get WAL + `busy_timeout` for the cross-process case.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from . import ids
from ..common.transitions import DecideFn
from .execution import ExecutionHandle
from .execution_records import ExecutionRecord
from .models import (
    ApprovalRecord,
    Artifact,
    ArtifactSummary,
    Attempt,
    Claim,
    Constraint,
    Decision,
    Event,
    ExternalRef,
    InboxItem,
    ManagerSession,
    PRIVACY_STRICTNESS,
    PrivacyTier,
    Review,
    SessionToken,
    Subtask,
    Task,
    TaskFilters,
    TaskSummary,
    WorkerRun,
    Workspace,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, priority TEXT NOT NULL, created_by TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    current_manager TEXT, handoff_summary TEXT,
    linear_issue_id TEXT, linear_issue_url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS constraints (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, text TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, manager_id TEXT NOT NULL,
    role TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
    released_at REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, made_by TEXT NOT NULL,
    decision TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT '',
    locked INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    superseded_by TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL, summary TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, artifact_refs_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, type TEXT NOT NULL, path TEXT NOT NULL,
    summary TEXT NOT NULL, created_by TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, requested_by TEXT NOT NULL,
    assigned_to TEXT NOT NULL, status TEXT NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '[]',
    result_artifact_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    delegation_id TEXT, parent_delegation_id TEXT
);
CREATE TABLE IF NOT EXISTS subtasks (
    id TEXT PRIMARY KEY, parent_task_id TEXT NOT NULL, title TEXT NOT NULL,
    instructions TEXT NOT NULL, status TEXT NOT NULL, privacy_tier TEXT NOT NULL,
    preferred_worker TEXT, assigned_worker TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    result_artifact_id TEXT, route_reason_json TEXT NOT NULL DEFAULT '{}',
    sandbox_json TEXT NOT NULL DEFAULT '{}',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    approved_by TEXT, approved_max_cost_usd REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    delegation_id TEXT, parent_delegation_id TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS worker_runs (
    id TEXT PRIMARY KEY, subtask_id TEXT NOT NULL, worker_id TEXT NOT NULL,
    harness TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
    route_reason_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL, finished_at REAL NOT NULL,
    input_artifact_id TEXT, output_artifact_id TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}', error TEXT
);
CREATE TABLE IF NOT EXISTS external_refs (
    id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    provider TEXT NOT NULL, external_id TEXT NOT NULL, external_url TEXT NOT NULL,
    sync_enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (entity_type, entity_id, provider)
);
CREATE TABLE IF NOT EXISTS inbox_items (
    id TEXT PRIMARY KEY, manager_id TEXT NOT NULL, kind TEXT NOT NULL,
    ref_id TEXT NOT NULL, task_id TEXT, title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, dismissed_at REAL NOT NULL,
    UNIQUE (manager_id, kind, ref_id)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, task_id TEXT, kind TEXT NOT NULL, actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, subtask_id TEXT NOT NULL,
    status TEXT NOT NULL, requested_worker TEXT NOT NULL, requested_location TEXT NOT NULL,
    estimated_cost_usd REAL NOT NULL, max_cost_usd REAL,
    decided_by TEXT, reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS executions (
    handle_id TEXT PRIMARY KEY, backend TEXT NOT NULL, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, workflow_id TEXT, run_id TEXT, state TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY, task_id TEXT, review_id TEXT, path TEXT NOT NULL,
    repo_path TEXT NOT NULL, artifact_path TEXT NOT NULL, repo_mode TEXT NOT NULL,
    created_at REAL NOT NULL, destroyed_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS manager_sessions (
    id TEXT PRIMARY KEY, task_id TEXT, review_id TEXT, manager_id TEXT NOT NULL,
    workspace_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL, claim_id TEXT,
    started_at REAL NOT NULL, ended_at REAL,
    launch_command TEXT NOT NULL, shell_command TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    delegation_id TEXT, parent_delegation_id TEXT
);
CREATE TABLE IF NOT EXISTS observation_handles (
    id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    task_id TEXT, provider TEXT NOT NULL, handle_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'unknown', outcome TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE (entity_type, entity_id, provider)
);
CREATE TABLE IF NOT EXISTS session_tokens (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    scope_json TEXT NOT NULL DEFAULT '{}', expires_at REAL, revoked_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_task ON workspaces(task_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_review ON workspaces(review_id);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON manager_sessions(task_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_review ON manager_sessions(review_id, status);
CREATE INDEX IF NOT EXISTS idx_tokens_session ON session_tokens(session_id);
CREATE INDEX IF NOT EXISTS idx_approvals_subtask ON approvals(subtask_id, status);
CREATE INDEX IF NOT EXISTS idx_executions_entity ON executions(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_executions_workflow ON executions(workflow_id);
CREATE INDEX IF NOT EXISTS idx_constraints_task ON constraints(task_id);
CREATE INDEX IF NOT EXISTS idx_claims_task ON claims(task_id);
CREATE INDEX IF NOT EXISTS idx_decisions_task ON decisions(task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_reviews_task ON reviews(task_id);
CREATE INDEX IF NOT EXISTS idx_reviews_assignee ON reviews(assigned_to);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_runs_subtask ON worker_runs(subtask_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_extrefs_lookup ON external_refs(provider, external_id);
CREATE INDEX IF NOT EXISTS idx_obs_entity ON observation_handles(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_obs_task ON observation_handles(task_id);
CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    outcome TEXT,
    actor TEXT NOT NULL DEFAULT '',
    task_id TEXT, subtask_id TEXT, run_id TEXT, review_id TEXT, session_id TEXT,
    delegation_id TEXT, parent_delegation_id TEXT, workspace_id TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_product TEXT NOT NULL DEFAULT 'agentconnect'
);
CREATE INDEX IF NOT EXISTS idx_eventlog_task ON event_log(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_eventlog_type ON event_log(type, seq);
CREATE INDEX IF NOT EXISTS idx_eventlog_subtask ON event_log(subtask_id, seq);
CREATE TABLE IF NOT EXISTS execution_records (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, subtask_id TEXT,
    work_request_id TEXT NOT NULL, decision_record_id TEXT NOT NULL,
    grant_id TEXT NOT NULL, correlation_id TEXT NOT NULL,
    outcome TEXT NOT NULL, record_json TEXT NOT NULL,
    record_hash TEXT NOT NULL, prev_hash TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execrec_task ON execution_records(task_id);
CREATE INDEX IF NOT EXISTS idx_execrec_grant ON execution_records(grant_id);
CREATE INDEX IF NOT EXISTS idx_execrec_correlation ON execution_records(correlation_id);
CREATE INDEX IF NOT EXISTS idx_execrec_decision ON execution_records(decision_record_id);
CREATE INDEX IF NOT EXISTS idx_execrec_wr ON execution_records(work_request_id);
"""
# NOTE: the idx_eventlog_source index is deliberately NOT in _SCHEMA. On a database
# created before source_product existed, `CREATE TABLE IF NOT EXISTS event_log` is a
# no-op (the table already exists without the column), so an index over
# source_product would fail with "no such column" during executescript — before
# _migrate() ever runs its ALTER. It is created in _migrate(), after the column is
# guaranteed to exist, so both fresh and upgraded databases build it correctly.

#: Columns added after the initial schema shipped. Existing databases created by
#: an earlier version are brought forward with ``ALTER TABLE ADD COLUMN`` at open
#: time — additive only, so a downgrade still reads the rows it understands. The
#: delegation ids make the agent tree and cross-entity correlation reconstructable
#: from the ledger without guessing from timestamps (observability handoff §Part IV).
_MIGRATIONS: dict[str, tuple[str, ...]] = {
    "subtasks": ("delegation_id", "parent_delegation_id", "depends_on_json"),
    "reviews": ("delegation_id", "parent_delegation_id"),
    "manager_sessions": ("delegation_id", "parent_delegation_id"),
}


def default_db_path() -> str:
    env = os.environ.get("AGENTCONNECT_DB_PATH")
    if env:
        return env
    return str(Path.home() / ".agentconnect" / "agentconnect.db")


def _scoped(column: str) -> str:
    """Match an entity's own rows, not a child's that names it.

    Sessions and workspaces created for a *review* store the parent `task_id` too.
    Selecting on `task_id` alone therefore also selects the reviewer's rows, which
    are newer — so `ORDER BY ... DESC LIMIT 1` returns the wrong one.
    """
    return f"{column}=?" + (" AND review_id IS NULL" if column == "task_id" else "")


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _u(text: Optional[str], fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return fallback


def _redact_ingested_payload(payload: Optional[dict], privacy_tier: Optional[str]) -> dict:
    """Fail-closed store-side re-redaction for a FOREIGN (multi-product
    publish ingress) event, before it is ever readable (docs/EVENT_BUS.md
    shared event bus contract v1, "PRIVACY"). Never trusts a publisher's own
    pre-redaction — this runs unconditionally, even for a payload that looks
    already clean.

    An unparseable or missing `privacy_tier` is treated as the strictest tier
    (`secret_sensitive`), the same asymmetric failure mode
    `observability.tree._safe_tier` uses for the same reason: leaking by
    default is the one failure a redaction boundary must never have.

    `local_only`/`secret_sensitive` (``PRIVACY_STRICTNESS >= 3``, matching
    every other write-time tier gate in this codebase —
    `AgentConnectService._tier_gated_free_text`, `_subtask_event_meta`)
    replace the WHOLE payload with a bounded marker, reusing the exact
    strings `/observe/tree` already withholds text with — one vocabulary for
    "this content was withheld", not two. Looser tiers still pass through the
    metadata scrubber (`SqliteEventLogProvider._scrub`) as defense in depth:
    dropped known-sensitive keys, masked credential-shaped keys, bounded
    strings — the same treatment every internally emitted event's metadata
    already gets before it can reach `event_log`.

    Local imports: `storage.py` is imported very early in
    `agentconnect.core`'s own `__init__`, before the observability
    subpackage is guaranteed to be fully initialized — a top-level import
    here would risk a circular-import order dependency neither module
    actually has today, but a local import costs nothing and stays safe if
    that ever changes.
    """
    from .observability.providers.event_log import _scrub
    from .observability.tree import _WITHHELD_TEXT

    try:
        tier = PrivacyTier(privacy_tier) if privacy_tier is not None else None
    except ValueError:
        tier = None
    if tier is None or PRIVACY_STRICTNESS.get(tier, 4) >= 3:
        marker = _WITHHELD_TEXT.get(tier, _WITHHELD_TEXT[PrivacyTier.secret_sensitive])
        return {"redacted": marker}
    return _scrub(payload or {})


class SqliteStorage:
    def __init__(self, path: str | os.PathLike[str] = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive schema migration for databases predating a column.

        Runs under the init lock. `observation_handles` and the fresh delegation
        columns are in `_SCHEMA` for new databases; here we bring an *existing*
        database forward by adding any missing column. Never drops or rewrites.
        """
        for table, columns in _MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column in columns:
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        # `observation_handles` may be absent in a pre-observability database even
        # though `_SCHEMA` (CREATE IF NOT EXISTS) just ran — it did create it. The
        # ALTER loop above is the only backfill needed.
        #
        # `event_log.source_product` (shared ecosystem event bus, multi-product
        # publish ingress — docs/EVENT_BUS.md) needs a NOT NULL DEFAULT, unlike
        # the generic nullable-TEXT columns the loop above adds, so it is not in
        # `_MIGRATIONS`: SQLite's `ALTER TABLE ADD COLUMN` accepts a non-null
        # default and backfills every existing row with it in the same
        # statement — every event ever written before this field existed was, in
        # fact, written by AgentConnect itself.
        event_log_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(event_log)").fetchall()
        }
        if "source_product" not in event_log_columns:
            self._conn.execute(
                "ALTER TABLE event_log ADD COLUMN source_product TEXT NOT NULL "
                "DEFAULT 'agentconnect'"
            )
        # Created here, not in _SCHEMA, so it is built only after the column above is
        # guaranteed present on both fresh and pre-source_product databases (see the
        # NOTE by _SCHEMA). IF NOT EXISTS keeps it idempotent across reopens.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eventlog_source "
            "ON event_log(source_product, seq)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialized read-modify-write span.

        Reentrant LOCK (the ``RLock`` nests fine) — but **not commit-reentrant**:
        the body still runs ``self._conn.commit()`` at every exit, including a
        nested one. Calling this again while already inside another
        ``transaction()``/``transition_row()`` span therefore commits the
        OUTER span's not-yet-finished work early. Never nest; compose a second
        write into an already-open span by accepting and using its ``conn``
        (see ``insert_claim(..., conn=conn)``, ``transition_row(..., conn=conn)``)
        instead of calling this again.
        """
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    #: `table -> (id_column, state_column)`. Internal constants, never caller
    #: input — the f-string interpolation in `transition_row` carries no
    #: injection surface (mirrors `status_counts`).
    _TRANSITION_TABLES: dict[str, tuple[str, str]] = {
        "tasks": ("id", "status"),
        "subtasks": ("id", "status"),
        "worker_runs": ("id", "status"),
        "reviews": ("id", "status"),
        "approvals": ("id", "status"),
        "manager_sessions": ("id", "status"),
        "executions": ("handle_id", "state"),
    }

    def _insert_transition_audit(self, conn: sqlite3.Connection, record: Any) -> None:
        """Raw INSERT on the caller's connection — never a self-committing
        helper — so a transition's audit row always rides the SAME commit as
        the state write it describes (fail-closed: "no state transition
        without a record").

        Also the Path 1 event-bus emission point (docs/EVENT_BUS.md): every
        *applied* transition additionally gets one ``state.changed`` row in
        ``event_log``, on the same connection, before the same commit — the
        guaranteed skeleton of the ecosystem event stream, independent of
        whether any observability provider is configured. Refused/noop
        outcomes stay legacy-audit-only, same as before.
        """
        from ..common.transitions import _default_message

        payload = {
            "vocabulary": record.vocabulary, "entity_id": record.entity_id,
            "src": record.src, "dst": record.dst, "outcome": record.outcome,
            "reason": record.reason, "message": _default_message(record),
        }
        conn.execute(
            "INSERT INTO events (id,task_id,kind,actor,payload_json,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (ids.new_id(ids.EVENT), record.task_id, "transition", record.actor,
             _j(payload), time.time()),
        )
        if record.outcome == "applied":
            corr = self._event_correlation_for_transition(conn, record)
            self._insert_event_row(
                conn, event_id=ids.new_id(ids.EVENT), type="state.changed",
                outcome=record.outcome, actor=record.actor,
                task_id=corr["task_id"], subtask_id=corr["subtask_id"],
                run_id=corr["run_id"], review_id=corr["review_id"],
                session_id=corr["session_id"], delegation_id=corr["delegation_id"],
                parent_delegation_id=corr["parent_delegation_id"],
                entity_id=record.entity_id,
                payload={"vocabulary": record.vocabulary, "src": record.src,
                         "dst": record.dst, "reason": (record.reason or "")[:300]},
            )

    #: `vocabulary name -> (table, columns to select for correlation)`. Internal
    #: constants, never caller input.
    _CORRELATION_LOOKUP: dict[str, tuple[str, tuple[str, ...]]] = {
        "subtasks": ("parent_task_id", ("delegation_id", "parent_delegation_id")),
        "subtask_status": ("subtasks", ("parent_task_id", "delegation_id",
                                        "parent_delegation_id")),
        "run_status": ("worker_runs", ("subtask_id",)),
        "session_status": ("manager_sessions", ("task_id", "delegation_id",
                                                "parent_delegation_id")),
        "review_status": ("reviews", ("task_id",)),
        "approval_status": ("approvals", ("task_id", "subtask_id")),
        "task_status": ("tasks", ("id",)),
    }

    def _event_correlation_for_transition(
        self, conn: sqlite3.Connection, record: Any,
    ) -> dict[str, Optional[str]]:
        """Enrich an audit record with the full correlation id set
        for `event_log`, via one same-connection SELECT keyed by
        ``record.vocabulary``. ``record.task_id`` is the fallback when the
        SELECT finds nothing (row already gone, or `execution_state`, whose
        entity ref stays in the payload only — see docs/EVENT_BUS.md)."""
        corr: dict[str, Optional[str]] = {
            "task_id": record.task_id, "subtask_id": None, "run_id": None,
            "review_id": None, "session_id": None, "delegation_id": None,
            "parent_delegation_id": None, "workspace_id": None,
        }
        if record.vocabulary == "task_status":
            corr["task_id"] = record.entity_id
            return corr
        lookup = self._CORRELATION_LOOKUP.get(record.vocabulary)
        if lookup is None:
            return corr
        table, columns = lookup
        try:
            row = conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} WHERE id=?",
                (record.entity_id,)
            ).fetchone()
        except sqlite3.Error:
            return corr
        if row is None:
            return corr
        if record.vocabulary == "subtask_status":
            corr["task_id"] = row["parent_task_id"] or corr["task_id"]
            corr["subtask_id"] = record.entity_id
            corr["delegation_id"] = row["delegation_id"]
            corr["parent_delegation_id"] = row["parent_delegation_id"]
        elif record.vocabulary == "run_status":
            corr["subtask_id"] = record.entity_id
            corr["run_id"] = row["subtask_id"]
        elif record.vocabulary == "session_status":
            corr["task_id"] = row["task_id"] or corr["task_id"]
            corr["session_id"] = record.entity_id
            corr["delegation_id"] = row["delegation_id"]
            corr["parent_delegation_id"] = row["parent_delegation_id"]
        elif record.vocabulary == "review_status":
            corr["task_id"] = record.entity_id
            corr["review_id"] = row["review_id"]
        elif record.vocabulary == "approval_status":
            corr["task_id"] = record.task_id or corr["task_id"]
            corr["subtask_id"] = record.entity_id
        return corr

    def _insert_event_row(
        self, conn: sqlite3.Connection, *, event_id: str, type: str,  # noqa: A002
        outcome: Optional[str] = None, actor: str = "",
        task_id: Optional[str] = None, subtask_id: Optional[str] = None,
        run_id: Optional[str] = None, review_id: Optional[str] = None,
        session_id: Optional[str] = None, delegation_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None, workspace_id: Optional[str] = None,
        entity_id: Optional[str] = None, payload: Optional[dict] = None,
        source_product: str = "agentconnect",
    ) -> bool:
        """Raw `INSERT OR IGNORE` on the caller's connection — the one write
        path every event-bus producer (§0: structural + rich + the multi-
        product publish ingress) shares. Returns whether a row was actually
        inserted (`False` on an `event_id` duplicate — idempotency, never a
        second door for a replayed id).

        `source_product` defaults to `"agentconnect"`, so every existing call
        site (Path 1's same-commit `state.changed`, Path 2's rich events) is
        unchanged and correctly attributed — every event they ever write
        genuinely originates inside AgentConnect. A foreign product's event
        (docs/EVENT_BUS.md shared event bus contract v1) passes its own value
        via `append_bus_event_ingested`, never through this default.
        """
        cur = conn.execute(
            "INSERT OR IGNORE INTO event_log (event_id,ts,type,outcome,actor,task_id,"
            "subtask_id,run_id,review_id,session_id,delegation_id,parent_delegation_id,"
            "workspace_id,entity_id,payload_json,source_product) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, time.time(), type, outcome, actor, task_id, subtask_id, run_id,
             review_id, session_id, delegation_id, parent_delegation_id, workspace_id,
             entity_id, _j(payload or {}), source_product),
        )
        return cur.rowcount == 1

    def append_bus_event(
        self, *, event_id: str, type: str,  # noqa: A002
        outcome: Optional[str] = None, actor: str = "",
        task_id: Optional[str] = None, subtask_id: Optional[str] = None,
        run_id: Optional[str] = None, review_id: Optional[str] = None,
        session_id: Optional[str] = None, delegation_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None, workspace_id: Optional[str] = None,
        entity_id: Optional[str] = None, payload: Optional[dict] = None,
        source_product: str = "agentconnect",
    ) -> Optional[int]:
        """Path 2 (rich, advisory) entry point — its own transaction, since
        the rich providers are called well after the ledger commit that
        triggered them. Returns the new `seq`, or `None` if `event_id` was
        already present (idempotent replay).

        `source_product` defaults to `"agentconnect"` — every internal caller
        (`SqliteEventLogProvider`, the Engine B bridge) is unaffected. The
        multi-product publish ingress (`append_bus_event_ingested`) is the
        only caller that ever passes a different value, and only after its
        own re-redaction pass.
        """
        with self.transaction() as c:
            inserted = self._insert_event_row(
                c, event_id=event_id, type=type, outcome=outcome, actor=actor,
                task_id=task_id, subtask_id=subtask_id, review_id=review_id,
                session_id=session_id, delegation_id=delegation_id,
                parent_delegation_id=parent_delegation_id,
                workspace_id=workspace_id, entity_id=entity_id,
                payload=payload, source_product=source_product,
            )
            if not inserted:
                return None
            row = c.execute("SELECT last_insert_rowid() AS seq").fetchone()
            return int(row["seq"])

    def append_bus_event_ingested(
        self, *, event_id: str, type: str, source_product: str,  # noqa: A002
        outcome: Optional[str] = None, actor: str = "",
        task_id: Optional[str] = None, subtask_id: Optional[str] = None,
        run_id: Optional[str] = None, review_id: Optional[str] = None,
        session_id: Optional[str] = None, delegation_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None, workspace_id: Optional[str] = None,
        entity_id: Optional[str] = None, payload: Optional[dict] = None,
        privacy_tier: Optional[str] = None,
    ) -> Optional[int]:
        """The multi-product publish ingress's ONE write path (`POST /events`,
        docs/EVENT_BUS.md shared event bus contract v1). The only difference
        from `append_bus_event`: `payload` is re-validated and re-redacted
        against `privacy_tier` HERE, fail-closed, before it is ever readable —
        a buggy or hostile publisher's own "pre-redaction" is never trusted.
        `source_product` is REQUIRED (no default): every ingested row must
        say, in the store's own words, who it came from.
        """
        redacted = _redact_ingested_payload(payload, privacy_tier)
        return self.append_bus_event(
            event_id=event_id, type=type, outcome=outcome, actor=actor,
            task_id=task_id, subtask_id=subtask_id, review_id=review_id,
            session_id=session_id, delegation_id=delegation_id,
            parent_delegation_id=parent_delegation_id, workspace_id=workspace_id,
            entity_id=entity_id, payload=redacted, source_product=source_product,
        )

    def list_bus_events(
        self, since: int = 0, limit: int = 100,
        types: Optional[list[str]] = None, task_id: Optional[str] = None,
        outcome: Optional[str] = None, source_products: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Replay/poll the canonical event stream. `since` is EXCLUSIVE (`seq
        > since`) — a consumer resumes with the last `seq` it saw, never with
        arithmetic on it, because `seq` is monotonic but not dense."""
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM event_log WHERE seq > ?"
        params: list[Any] = [since]
        if types:
            marks = ",".join("?" * len(types))
            sql += f" AND type IN ({marks})"
            params.extend(types)
        if task_id:
            sql += " AND task_id=?"
            params.append(task_id)
        if outcome:
            sql += " AND outcome=?"
            params.append(outcome)
        if source_products:
            marks = ",".join("?" * len(source_products))
            sql += f" AND source_product IN ({marks})"
            params.extend(source_products)
        sql += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._bus_event(r) for r in rows]

    def latest_bus_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM event_log"
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _bus_event(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": r["seq"], "event_id": r["event_id"], "type": r["type"],
            "outcome": r["outcome"], "actor": r["actor"], "task_id": r["task_id"],
            "subtask_id": r["subtask_id"], "run_id": r["run_id"],
            "review_id": r["review_id"], "session_id": r["session_id"],
            "delegation_id": r["delegation_id"],
            "parent_delegation_id": r["parent_delegation_id"],
            "workspace_id": r["workspace_id"], "entity_id": r["entity_id"],
            "payload": _u(r["payload_json"], {}),
            "source_product": r["source_product"],
        }

    @staticmethod
    def _reject_state_write(fields: dict, key: str, table: str) -> None:
        """Enforcement (goal item 2/8): a state/status key must go through
        :meth:`transition_row` (the authority's writer), never a bare
        ``update_*`` call. Grep-provable statically (no call site under
        ``packages/*/src`` passes one) AND runtime-provable here."""
        if key in fields:
            raise ValueError(
                f"{table}.{key} must go through TransitionAuthority "
                "(SqliteStorage.transition_row), not update_*()"
            )

    @staticmethod
    def _normalize_transition_fields(fields: dict) -> dict:
        """Same friendly-key -> real-column normalization every hand-written
        ``update_*`` method already does (``metadata`` -> ``metadata_json``,
        ``route_reason`` -> ``route_reason_json``, ``depends_on`` ->
        ``depends_on_json``, ``criteria`` -> ``criteria_json``) — centralized
        here so a `decide()` closure in service.py can pass the same field
        names it would to `update_subtask`/`update_task` without knowing
        which underlying column is JSON-encoded."""
        fields = dict(fields)
        if "metadata" in fields:
            fields["metadata_json"] = _j(fields.pop("metadata"))
        if "route_reason" in fields:
            fields["route_reason_json"] = _j(fields.pop("route_reason"))
        if "depends_on" in fields:
            fields["depends_on_json"] = _j(fields.pop("depends_on"))
        if "criteria" in fields:
            fields["criteria_json"] = _j(fields.pop("criteria"))
        if "metrics" in fields:
            fields["metrics_json"] = _j(fields.pop("metrics"))
        return fields

    def transition_row(
        self, table: str, entity_id: str, decide: DecideFn,
        conn: Optional[sqlite3.Connection] = None,
    ) -> tuple[bool, Optional[str]]:
        """The generic :class:`~agentconnect.common.transitions.LockedWriter`
        for every Engine-A table. Reads the row's state/status column fresh,
        calls ``decide`` with it, and — if it returns fields to write —
        applies one guarded ``UPDATE ... WHERE {id_col}=? AND {state_col}=?``
        (exact-match CAS against the value just read) plus, if a record was
        returned, an audit INSERT, all on the same connection.

        When ``conn`` is given (the ``claim_task`` composition path), this
        never commits — the caller's own :meth:`transaction` span commits
        everything together. When ``conn`` is ``None`` it opens (and commits)
        its own span, retrying up to 3 times on a CAS miss (a cross-process
        writer only; every in-process writer already serializes on
        ``self._lock``).
        """
        id_col, state_col = self._TRANSITION_TABLES[table]

        def _attempt(c: sqlite3.Connection) -> tuple[bool, Optional[str], bool]:
            row = c.execute(
                f"SELECT {state_col} FROM {table} WHERE {id_col}=?", (entity_id,)
            ).fetchone()
            current_raw = row[state_col] if row is not None else None
            fields, record = decide(current_raw)
            if fields is not None:
                fields = self._normalize_transition_fields(fields)
            if fields is None:
                if record is not None:
                    self._insert_transition_audit(c, record)
                return False, current_raw, True
            cols = ", ".join(f"{k} = ?" for k in fields)
            cur = c.execute(
                f"UPDATE {table} SET {cols} WHERE {id_col} = ? AND {state_col} = ?",
                (*fields.values(), entity_id, current_raw),
            )
            if cur.rowcount != 1:
                return False, current_raw, False
            if record is not None:
                self._insert_transition_audit(c, record)
            return True, fields.get(state_col, current_raw), True

        if conn is not None:
            applied, stored, _done = _attempt(conn)
            return applied, stored

        applied, stored, done = False, None, False
        for _attempt_no in range(3):
            with self.transaction() as c:
                applied, stored, done = _attempt(c)
            if done:
                return applied, stored
        return applied, stored

    # -------------------------------------------------------------- tasks
    def insert_task(self, task: Task) -> Task:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO tasks (id,title,goal,status,priority,created_by,created_at,"
                "updated_at,current_manager,handoff_summary,linear_issue_id,linear_issue_url,"
                "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, task.title, task.goal, task.status.value, task.priority.value,
                 task.created_by, task.created_at, task.updated_at, task.current_manager,
                 task.handoff_summary, task.linear_issue_id, task.linear_issue_url,
                 _j(task.metadata)),
            )
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "tasks")
        if "metadata" in fields:
            fields["metadata_json"] = _j(fields.pop("metadata"))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def _list_task_rows(self, filters: TaskFilters) -> list[sqlite3.Row]:
        sql = "SELECT * FROM tasks"
        where, params = [], []
        if filters.status:
            where.append("status=?")
            params.append(filters.status.value)
        if filters.current_manager:
            where.append("current_manager=?")
            params.append(filters.current_manager)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params += [max(0, filters.limit), max(0, filters.offset)]
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def list_tasks(self, filters: TaskFilters) -> list[TaskSummary]:
        return [
            TaskSummary(
                id=r["id"], title=r["title"], status=r["status"], priority=r["priority"],
                current_manager=r["current_manager"], updated_at=r["updated_at"],
                linear_issue_url=r["linear_issue_url"],
            )
            for r in self._list_task_rows(filters)
        ]

    def list_tasks_full(self, filters: TaskFilters) -> list[Task]:
        """Same query as `list_tasks`, materialized as full `Task` rows. For
        callers (observe-tree assembly) that need every column of many tasks at
        once — one SELECT, never a per-summary `get_task` re-read (the N+1 the
        first tree implementation had)."""
        return [self._task(r) for r in self._list_task_rows(filters)]

    @staticmethod
    def _task(r: sqlite3.Row) -> Task:
        return Task(
            id=r["id"], title=r["title"], goal=r["goal"], status=r["status"],
            priority=r["priority"], created_by=r["created_by"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            current_manager=r["current_manager"], handoff_summary=r["handoff_summary"],
            linear_issue_id=r["linear_issue_id"], linear_issue_url=r["linear_issue_url"],
            metadata=_u(r["metadata_json"], {}),
        )

    # --------------------------------------------------------- constraints
    def insert_constraint(self, c_: Constraint) -> Constraint:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO constraints (id,task_id,text,created_by,created_at) VALUES (?,?,?,?,?,?)",
                (c_.id, c_.task_id, c_.text, c_.created_by, c_.created_at),
            )
        return c_

    def list_constraints(self, task_id: str) -> list[Constraint]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM constraints WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [Constraint(**dict(r)) for r in rows]

    # -------------------------------------------------------------- claims
    def insert_claim(self, claim: Claim, conn: Optional[sqlite3.Connection] = None) -> Claim:
        sql = ("INSERT INTO claims (id,task_id,manager_id,role,expires_at,created_at,released_at)"
               " VALUES (?,?,?,?,?,?,?)")
        args = (claim.id, claim.task_id, claim.manager_id, claim.role.value,
                claim.expires_at, claim.created_at, claim.released_at)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.transaction() as c:
                c.execute(sql, args)
        return claim

    def active_claims(self, task_id: str, at: float,
                      conn: Optional[sqlite3.Connection] = None) -> list[Claim]:
        c = conn or self._conn
        with self._lock:
            rows = c.execute(
                "SELECT * FROM claims WHERE task_id=? AND released_at IS NULL AND expires_at > ?"
                " ORDER BY created_at",
                (task_id, at),
            ).fetchall()
        return [self._claim(r) for r in rows]

    def list_claims(self, task_id: str) -> list[Claim]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM claims WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [self._claim(r) for r in rows]

    def release_claims(self, manager_id: str, task_id: str, at: float) -> int:
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE claims SET released_at=? WHERE task_id=? AND manager_id=?"
                " AND released_at IS NULL",
                (at, task_id, manager_id),
            )
            return cur.rowcount

    @staticmethod
    def _claim(r: sqlite3.Row) -> Claim:
        return Claim(
            id=r["id"], task_id=r["task_id"], manager_id=r["manager_id"], role=r["role"],
            expires_at=r["expires_at"], created_at=r["created_at"],
            released_at=r["released_at"],
        )

    # ----------------------------------------------------------- decisions
    def insert_decision(self, d: Decision, conn: Optional[sqlite3.Connection] = None) -> Decision:
        sql = ("INSERT INTO decisions (id,task_id,made_by,decision,rationale,locked,created_at,"
               "superseded_by) VALUES (?,?,?,?,?,?,?,?)")
        args = (d.id, d.task_id, d.made_by, d.decision, d.rationale, int(d.locked),
                d.created_at, d.superseded_by)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.transaction() as c:
                c.execute(sql, args)
        return d

    def get_decision(self, decision_id: str,
                     conn: Optional[sqlite3.Connection] = None) -> Optional[Decision]:
        c = conn or self._conn
        with self._lock:
            row = c.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return self._decision(row) if row else None

    def list_decisions(self, task_id: str) -> list[Decision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [self._decision(r) for r in rows]

    def mark_superseded(self, decision_id: str, by: str,
                        conn: Optional[sqlite3.Connection] = None) -> None:
        sql = "UPDATE decisions SET superseded_by=? WHERE id=?"
        args = (by, decision_id)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.transaction() as c:
                c.execute(sql, args)

    @staticmethod
    def _decision(r: sqlite3.Row) -> Decision:
        return Decision(
            id=r["id"], task_id=r["task_id"], made_by=r["made_by"], decision=r["decision"],
            rationale=r["rationale"], locked=bool(r["locked"]), created_at=r["created_at"],
            updated_at=r.get("updated_at") if hasattr(r, "get") else None,
            superseded_by=r["superseded_by"],
        )

    # ------------------------------------------------------------ attempts
    def insert_attempt(self, a: Attempt) -> Attempt:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO attempts (id,task_id,actor_id,actor_type,summary,outcome,created_at,"
                "artifact_refs_json) VALUES (?,?,?,?,?,?,?,?)",
                (a.id, a.task_id, a.actor_id, a.actor_type.value, a.summary, a.outcome,
                 a.created_at, _j(a.artifact_refs)),
            )
        return a

    def list_attempts(self, task_id: str, limit: Optional[int] = None) -> list[Attempt]:
        sql = "SELECT * FROM attempts WHERE task_id=? ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, (task_id,)).fetchall()
        out = [
            Attempt(
                id=r["id"], task_id=r["task_id"], actor_id=r["actor_id"],
                actor_type=r["actor_type"], summary=r["summary"], outcome=r["outcome"],
                created_at=r["created_at"], artifact_refs=_u(r["artifact_refs_json"], []),
            )
            for r in rows
        ]
        return out[-limit:] if limit else out

    # ----------------------------------------------------------- artifacts
    def insert_artifact(self, a: Artifact) -> Artifact:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO artifacts (id,task_id,type,path,summary,created_by,created_at,"
                "size_bytes,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (a.id, a.task_id, a.type.value, a.path, a.summary, a.created_by,
                 a.created_at, a.size_bytes, _j(a.metadata)),
            )
        return a

    def get_artifact(self, artifact_id: str) -> Optional[Artifact]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
        if not row:
            return None
        return Artifact(
            id=row["id"], task_id=row["task_id"], type=row["type"], path=row["path"],
            summary=row["summary"], created_by=row["created_by"],
            created_at=row["created_at"], size_bytes=row["size_bytes"],
            metadata=_u(row["metadata_json"], {}),
        )

    def list_artifacts(self, task_id: str) -> list[ArtifactSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [
            ArtifactSummary(
                id=r["id"], task_id=r["task_id"], type=r["type"], path=r["path"],
                summary=r["summary"], created_by=r["created_by"], created_at=r["created_at"],
                size_bytes=r["size_bytes"], metadata=_u(r["metadata_json"], {}),
            )
            for r in rows
        ]

    # ------------------------------------------------------------- reviews
    def insert_review(self, rv: Review) -> Review:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO reviews (id,task_id,requested_by,requested_by,assigned_to,status,criteria_json,"
                "artifact_refs_json,result_artifact_id,created_at,updated_at,"
                "delegation_id,parent_delegation_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rv.id, rv.task_id, rv.requested_by, rv.assigned_to, rv.status.value,
                 _j(rv.criteria), _j(rv.artifact_refs), rv.result_artifact_id,
                 rv.created_at, rv.updated_at, rv.delegation_id, rv.parent_delegation_id),
            )
        return rv

    def get_review(self, review_id: str,
                   conn: Optional[sqlite3.Connection] = None) -> Optional[Review]:
        c = conn or self._conn
        with self._lock:
            row = c.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        return self._review(row) if row else None

    def update_review(self, review_id: str, conn: Optional[sqlite3.Connection] = None,
                      **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "reviews")
        cols = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE reviews SET {cols} WHERE id=?"
        args = (*fields.values(), review_id)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.transaction() as c:
                c.execute(sql, args)

    def list_reviews(self, task_id: str) -> list[Review]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reviews WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [self._review(r) for r in rows]

    def reviews_for_manager(self, manager_id: str, statuses: tuple[str, ...]) -> list[Review]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM reviews WHERE assigned_to=? AND status IN ({marks})"
                " ORDER BY created_at",
                (manager_id, *statuses),
            ).fetchall()
        return [self._review(r) for r in rows]

    @staticmethod
    def _review(r: sqlite3.Row) -> Review:
        return Review(
            id=r["id"], task_id=r["task_id"], requested_by=r["requested_by"],
            assigned_to=r["assigned_to"], status=r["status"],
            criteria=_u(r["criteria_json"], []), artifact_refs=_u(r["artifact_refs_json"], []),
            result_artifact_id=r["result_artifact_id"], created_at=r["created_at"],
            updated_at=r["updated_at"],
            delegation_id=r["delegation_id"], parent_delegation_id=r["parent_delegation_id"],
        )

    # ------------------------------------------------------------ subtasks
    def insert_subtask(self, s: Subtask) -> Subtask:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO subtasks (id,parent_task_id,title,instructions,status,privacy_tier,"
                "preferred_worker,assigned_worker,"
                "created_at,updated_at,result_artifact_id,route_reason_json,"
                "sandbox_json,required_capabilities_json,approved_by,approved_max_cost_usd,"
                "metadata_json,delegation_id,parent_delegation_id,depends_on_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.id, s.parent_task_id, s.title, s.instructions, s.status.value,
                 s.privacy_tier.value, s.preferred_worker, s.assigned_worker,
                 s.created_at, s.updated_at, s.result_artifact_id, _j(s.route_reason),
                 _j(s.sandbox.model_dump(mode="json")), _j(s.required_capabilities),
                 s.approved_by, s.approved_max_cost_usd, _j(s.metadata),
                 s.delegation_id, s.parent_delegation_id, _j(s.depends_on)),
            )
        return s

    def get_subtask(self, subtask_id: str) -> Optional[Subtask]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subtasks WHERE id=?",
                (subtask_id,),
            ).fetchone()
        return self._subtask(row) if row else None

    def update_subtask(self, subtask_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "subtasks")
        if "metadata" in fields:
            fields["metadata_json"] = _j(fields.pop("metadata"))
        if "route_reason" in fields:
            fields["route_reason_json"] = _j(fields.pop("route_reason"))
        if "depends_on" in fields:
            fields["depends_on_json"] = _j(fields.pop("depends_on"))
        if "criteria" in fields:
            fields["criteria_json"] = _j(fields.pop("criteria"))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE subtasks SET {cols} WHERE id=?", (*fields.values(), subtask_id))

    # NOTE: the old boolean `update_subtask_if_status` / `update_run_if_status`
    # CAS helpers were REMOVED (consolidation review finding): after every call
    # site migrated onto the `subtask_status` / `run_status` authorities they
    # were orphaned public methods that could still flip a status column with
    # no FSM check and no audit row — a second door past the one transition
    # authority. `transition_row` is the only status writer for these tables.

    def list_subtasks(self, task_id: str) -> list[Subtask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM subtasks WHERE parent_task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [self._subtask(r) for r in rows]

    @staticmethod
    def _subtask(r: sqlite3.Row) -> Subtask:
        return Subtask(
            id=r["id"], parent_task_id=r["parent_task_id"], title=r["title"],
            instructions=r["instructions"], status=r["status"], privacy_tier=r["privacy_tier"],
            preferred_worker=r["preferred_worker"], assigned_worker=r["assigned_worker"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            result_artifact_id=r["result_artifact_id"],
            route_reason=_u(r["route_reason_json"], {}),
            sandbox=_u(r["sandbox_json"], {}),
            required_capabilities=_u(r["required_capabilities_json"], []),
            approved_by=r["approved_by"], approved_max_cost_usd=r["approved_max_cost_usd"],
            metadata=_u(r["metadata_json"], {}),
            #: NULL on a database migrated forward from before this column existed
            #: (a plain `ALTER TABLE ADD COLUMN`, backfilled with no default);
            #: `_u` treats that exactly like an absent key: no dependencies.
            depends_on=_u(r["depends_on_json"], []),
        )

    # --------------------------------------------------------- worker runs
    def insert_run(self, run: WorkerRun) -> WorkerRun:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO worker_runs (id,subtask_id,worker_id,harness,model,status,"
                "route_reason_json,started_at,finished_at,input_artifact_id,output_artifact_id,"
                "metrics_json,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run.id, run.subtask_id, run.worker_id, run.harness, run.model,
                 run.status.value, _j(run.route_reason), run.started_at, run.finished_at,
                 run.input_artifact_id, run.output_artifact_id, _j(run.metrics), run.error),
            )
        return run

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "worker_runs")
        if "metrics" in fields:
            fields["metrics_json"] = _j(fields.pop("metrics"))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE worker_runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def total_run_cost_usd(self) -> float:
        """Cumulative actual spend recorded across ALL worker runs (the
        ``estimated_cost_usd`` metric each paid worker reports after really
        running). Feeds the cumulative spend cap in routing — per-call estimate
        gating alone lets unbounded spend accrue one approved call at a time."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(CAST(json_extract(metrics_json,"
                " '$.estimated_cost_usd') AS REAL)), 0.0) AS total FROM worker_runs"
            ).fetchone()
        return float(row["total"] or 0.0)

    def list_runs(self, subtask_id: str) -> list[WorkerRun]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM worker_runs WHERE subtask_id=? ORDER BY started_at", (subtask_id,)
            ).fetchall()
        return [self._run(r) for r in rows]

    def list_runs_for_task(self, task_id: str) -> list[WorkerRun]:
        """Every run under every subtask of one task, in one query (join over
        `idx_runs_subtask`/`idx_subtasks_task`). The batched read the
        observe-tree assembly uses instead of one `list_runs` per subtask."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT wr.* FROM worker_runs wr JOIN subtasks s ON wr.subtask_id = s.id"
                " WHERE s.parent_task_id=? ORDER BY wr.started_at",
                (task_id,),
            ).fetchall()
        return [self._run(r) for r in rows]

    def get_run(self, run_id: str) -> Optional[WorkerRun]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM worker_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        return self._run(row) if row else None

    def list_runs_by_status(self, status: str, limit: int = 1000) -> list[WorkerRun]:
        """Every run in a given status across all subtasks. Used by the orphan
        reconcile pass to find `running` runs whose process may have died."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM worker_runs WHERE status=? ORDER BY started_at LIMIT ?",
                (status, max(0, limit)),
            ).fetchall()
        return [self._run(r) for r in rows]

    @staticmethod
    def _run(r: sqlite3.Row) -> WorkerRun:
        return WorkerRun(
            id=r["id"], subtask_id=r["subtask_id"], worker_id=r["worker_id"],
            harness=r["harness"], model=r["model"], status=r["status"],
            route_reason=_u(r["route_reason_json"], {}), started_at=r["started_at"],
            finished_at=r["finished_at"], input_artifact_id=r["input_artifact_id"],
            output_artifact_id=r["output_artifact_id"], metrics=_u(r["metrics_json"], {}),
            error=r["error"],
        )

    # ------------------------------------------------------- external refs
    def upsert_external_ref(self, ref: ExternalRef) -> ExternalRef:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO external_refs (id,entity_type,entity_id,provider,external_id,"
                "external_url,sync_enabled,created_at,updated_at,metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(entity_type,entity_id,provider) DO UPDATE SET"
                " external_id=excluded.external_id, external_url=excluded.external_url,"
                " sync_enabled=excluded.sync_enabled, updated_at=excluded.updated_at,"
                " metadata_json=excluded.metadata_json",
                (ref.id, ref.entity_type, ref.entity_id, ref.provider,
                 ref.external_id, ref.external_url, int(ref.sync_enabled),
                 ref.created_at, ref.updated_at, _j(ref.metadata)),
            )
        return self.get_external_ref(ref.entity_type, ref.entity_id, ref.provider) or ref

    def get_external_ref(self, entity_type: str, entity_id: str,
                         provider: str) -> Optional[ExternalRef]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM external_refs WHERE entity_type=? AND entity_id=? AND provider=?",
                (entity_type, entity_id, provider),
            ).fetchone()
        return self._extref(row) if row else None

    def find_by_external_id(self, provider: str, external_id: str) -> Optional[ExternalRef]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM external_refs WHERE provider=? AND external_id=?",
                (provider, external_id),
            ).fetchone()
        return self._extref(row) if row else None

    @staticmethod
    def _extref(r: sqlite3.Row) -> ExternalRef:
        return ExternalRef(
            id=r["id"], entity_type=r["entity_type"], entity_id=r["entity_id"],
            provider=r["provider"], external_id=r["external_id"], external_url=r["external_url"],
            sync_enabled=bool(r["sync_enabled"]), created_at=r["created_at"],
            updated_at=r["updated_at"], metadata=_u(r["metadata_json"], {}),
        )

    # --------------------------------------------------------------- inbox
    def insert_inbox_item(self, item: InboxItem) -> InboxItem:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO inbox_items (id,manager_id,kind,ref_id,task_id,title,created_at,"
                "dismissed_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(manager_id,kind,ref_id) DO NOTHING",
                (item.id, item.manager_id, item.kind.value, item.ref_id, item.task_id,
                 item.title, item.created_at, item.dismissed_at),
            )
        return item

    def list_inbox_items(self, manager_id: str) -> list[InboxItem]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM inbox_items WHERE manager_id=? AND dismissed_at IS NULL"
                " ORDER BY created_at",
                (manager_id,),
            ).fetchall()
        return [
            InboxItem(
                id=r["id"], manager_id=r["manager_id"], kind=r["kind"],
                ref_id=r["ref_id"], task_id=r["task_id"], title=r["title"],
                created_at=r["created_at"], dismissed_at=r["dismissed_at"],
            )
            for r in rows
        ]

    def dismiss_inbox_items(self, manager_id: str, at: float) -> int:
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE inbox_items SET dismissed_at=? WHERE manager_id=? AND dismissed_at IS NULL",
                (at, manager_id),
            )
            return cur.rowcount

    # ----------------------------------------------------------- approvals
    def insert_approval(self, a: ApprovalRecord) -> ApprovalRecord:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO approvals (id,task_id,subtask_id,status,requested_worker,"
                "requested_location,estimated_cost_usd,max_cost_usd,decided_by,reason,"
                "created_at,decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.task_id, a.subtask_id, a.status.value, a.requested_worker,
                 a.requested_location, a.estimated_cost_usd, a.max_cost_usd,
                 a.decided_by, a.reason, a.created_at, a.decided_at),
            )
        return a

    def get_approval(self, approval_id: str) -> Optional[ApprovalRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
        return self._approval(row) if row else None

    def pending_approval_for(self, subtask_id: str) -> Optional[ApprovalRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE subtask_id=? AND status='pending'"
                " ORDER BY created_at DESC LIMIT 1",
                (subtask_id,),
            ).fetchone()
        return self._approval(row) if row else None

    def list_approvals(self, task_id: str) -> list[ApprovalRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        return [self._approval(r) for r in rows]

    def update_approval(self, approval_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "approvals")
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE approvals SET {cols} WHERE id=?", (*fields.values(), approval_id))

    @staticmethod
    def _approval(r: sqlite3.Row) -> ApprovalRecord:
        return ApprovalRecord(
            id=r["id"], task_id=r["task_id"], subtask_id=r["subtask_id"], status=r["status"],
            requested_worker=r["requested_worker"], requested_location=r["requested_location"],
            estimated_cost_usd=r["estimated_cost_usd"], max_cost_usd=r["max_cost_usd"],
            decided_by=r["decided_by"], reason=r["reason"], created_at=r["created_at"],
            decided_at=r["decided_at"],
        )

    # ---------------------------------------------------------- executions
    def upsert_execution(self, h: ExecutionHandle) -> ExecutionHandle:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO executions (handle_id,backend,entity_type,entity_id,workflow_id,"
                "run_id,state,created_at,updated_at,detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(handle_id) DO UPDATE SET workflow_id=excluded.workflow_id,"
                " run_id=excluded.run_id, state=excluded.state,"
                " updated_at=excluded.updated_at, detail=excluded.detail",
                (h.handle_id, h.backend, h.entity_type, h.entity_id, h.workflow_id,
                 h.run_id, h.state.value, h.created_at, h.updated_at, h.detail),
            )
        return self.get_execution(h.handle_id) or h

    def get_execution(self, handle_id: str) -> Optional[ExecutionHandle]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE handle_id=? OR workflow_id=?",
                (handle_id, handle_id),
            ).fetchone()
        return self._execution(row) if row else None

    def executions_for(self, entity_type: str, entity_id: str) -> list[ExecutionHandle]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE entity_type=? AND entity_id=? ORDER BY created_at",
                (entity_type, entity_id),
            ).fetchall()
        return [self._execution(r) for r in rows]

    def update_execution(self, handle_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "state", "executions")
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(
                f"UPDATE executions SET {cols} WHERE handle_id=? OR workflow_id=?",
                (*fields.values(), handle_id, handle_id),
            )

    @staticmethod
    def _execution(r: sqlite3.Row) -> ExecutionHandle:
        return ExecutionHandle(
            handle_id=r["handle_id"], backend=r["backend"], entity_type=r["entity_type"],
            entity_id=r["entity_id"], workflow_id=r["workflow_id"], run_id=r["run_id"],
            state=r["state"], created_at=r["created_at"], updated_at=r["updated_at"],
            detail=r["detail"],
        )

    # -------------------------------------------------- execution records
    # R6 Execution Records (ADR-037 layer-3 evidence). Append-only: there is no
    # update path — like decisions/attempts, a record is written once and only
    # ever read. `record_json` is the full canonical record (the hashable
    # projection plus the seal); the lifted columns exist so the chain
    # work-request → decision → grant → redemption → execution is traversable
    # in both directions by id without parsing JSON.
    def insert_execution_record(self, r: ExecutionRecord, created_at: float,
                                conn: Optional[sqlite3.Connection] = None) -> ExecutionRecord:
        sql = ("INSERT INTO execution_records (id,task_id,subtask_id,work_request_id,"
               "decision_record_id,grant_id,correlation_id,outcome,record_json,"
               "record_hash,prev_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
        args = (r.execution_record_id, r.task_id, r.subtask_id, r.work_request_id,
                r.decision_record_id, r.grant_id, r.correlation_id, r.outcome,
                _j(r.model_dump(mode="json")), r.record_hash, r.prev_hash, created_at)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.transaction() as c:
                c.execute(sql, args)
        return r

    def get_execution_record(self, execution_record_id: str) -> Optional[ExecutionRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT record_json FROM execution_records WHERE id=?",
                (execution_record_id,),
            ).fetchone()
        return self._execution_record(row) if row else None

    def get_execution_record_by_grant(self, grant_id: str) -> list[ExecutionRecord]:
        return self.list_execution_records(grant_id=grant_id)

    def list_execution_records(
        self, task_id: Optional[str] = None, *,
        grant_id: Optional[str] = None,
        decision_record_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        work_request_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        """Newest-last records matching every supplied linkage id (AND semantics).
        This is the bidirectional-traversal read: any one id in the chain finds
        the record that names all the others."""
        clauses, args = [], []
        for column, value in (
            ("task_id", task_id), ("grant_id", grant_id),
            ("decision_record_id", decision_record_id),
            ("correlation_id", correlation_id), ("work_request_id", work_request_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT record_json FROM execution_records{where}"
                " ORDER BY created_at, rowid LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [self._execution_record(r) for r in rows]

    def list_execution_record_chain(self) -> list[ExecutionRecord]:
        """Every execution record in ledger insertion order (oldest first).

        ``list_execution_records`` caps at ``limit`` for traversal reads; chain
        verification must walk the whole table, so it reads without a LIMIT.
        Ordered by ``rowid`` — the same insertion order the chain was written
        in (``latest_execution_record_hash`` reads the head the same way) —
        because the app-layer ``created_at`` clock is not guaranteed monotone.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM execution_records ORDER BY rowid"
            ).fetchall()
        return [self._execution_record(r) for r in rows]

    def latest_execution_record_hash(self,
                                     conn: Optional[sqlite3.Connection] = None
                                     ) -> Optional[str]:
        """The current head of the ledger's execution-record hash chain (None on
        an empty ledger). Pass the writer's transaction connection so the chain
        cannot fork under concurrency (same discipline as insert_decision)."""
        c = conn or self._conn
        with self._lock:
            row = c.execute(
                "SELECT record_hash FROM execution_records ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return str(row["record_hash"]) if row else None

    @staticmethod
    def _execution_record(r: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord.model_validate(_u(r["record_json"], {}))

    # ---------------------------------------------------------- workspaces
    def insert_workspace(self, w: Workspace) -> Workspace:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO workspaces (id,task_id,review_id,path,repo_path,artifact_path,"
                "repo_mode,created_at,destroyed_at,metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (w.id, w.task_id, w.review_id, w.path, w.repo_path, w.artifact_path,
                 w.repo_mode.value, w.created_at, w.destroyed_at, _j(w.metadata)),
            )
        return w

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspaces WHERE id=?",
                (workspace_id,),
            ).fetchone()
        return self._workspace(row) if row else None

    def find_workspace(
        self, task_id: Optional[str] = None, review_id: Optional[str] = None
    ) -> Optional[Workspace]:
        """The live workspace for an entity. A destroyed one never matches.

        A *review's* workspace also carries its parent `task_id`, and it is created
        later — so a plain `task_id` match, newest first, hands back the reviewer's
        empty checkout instead of the manager's worktree. `_scoped` excludes it.
        """
        column, value = ("review_id", review_id) if review_id else ("task_id", task_id)
        if not value:
            return None
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM workspaces WHERE {_scoped(column)} AND destroyed_at IS NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (value,),
            ).fetchone()
        return self._workspace(row) if row else None

    def list_workspaces(self, include_destroyed: bool = False) -> list[Workspace]:
        sql = "SELECT * FROM workspaces"
        if not include_destroyed:
            sql += " WHERE destroyed_at IS NULL"
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._workspace(r) for r in rows]

    def update_workspace(self, workspace_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "workspaces")
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE workspaces SET {cols} WHERE id=?",
                      (*fields.values(), workspace_id))

    @staticmethod
    def _workspace(r: sqlite3.Row) -> Workspace:
        return Workspace(
            id=r["id"], task_id=r["task_id"], review_id=r["review_id"], path=r["path"],
            repo_path=r["repo_path"], artifact_path=r["artifact_path"],
            repo_mode=r["repo_mode"], created_at=r["created_at"],
            destroyed_at=r["destroyed_at"], metadata=_u(r["metadata_json"], {}),
        )

    # ------------------------------------------------------ manager sessions
    def insert_session(self, s: ManagerSession) -> ManagerSession:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO sessions (id,task_id,review_id,manager_id,workspace_id,"
                "mode,status,claim_id,started_at,ended_at,launch_command,shell_command,"
                "metadata_json,delegation_id,parent_delegation_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.id, s.task_id, s.review_id, s.manager_id, s.workspace_id,
                 s.mode.value, s.status.value, s.claim_id, s.started_at, s.ended_at,
                 s.launch_command, s.shell_command, _j(s.metadata),
                 s.delegation_id, s.parent_delegation_id),
            )
        return s

    def get_session(self, session_id: str) -> Optional[ManagerSession]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM manager_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return self._session(row) if row else None

    def latest_session(
        self, task_id: Optional[str] = None, review_id: Optional[str] = None,
        statuses: Optional[tuple[str, ...]] = None,
    ) -> Optional[ManagerSession]:
        """The newest session *on* an entity — never one that merely mentions it.

        A reviewer's session stores the parent `task_id` alongside its `review_id`,
        and it starts after the manager is done. Without `_scoped`, the task's
        "current session" becomes the reviewer's, every attempt the manager
        recorded now predates it, and `audit_task` reports that the agent recorded
        nothing. Requesting a review would make a task uncompletable.
        """
        column, value = ("review_id", review_id) if review_id else ("task_id", task_id)
        if not value:
            return None
        sql = f"SELECT * FROM manager_sessions WHERE {_scoped(column)}"
        params: list[Any] = [value]
        if statuses:
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            params.extend(statuses)
        sql += " ORDER BY started_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return self._session(row) if row else None

    def list_sessions(
        self, task_id: Optional[str] = None, manager_id: Optional[str] = None,
        status: Optional[str] = None, limit: int = 50,
    ) -> list[ManagerSession]:
        clauses, params = [], []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if manager_id:
            clauses.append("manager_id=?")
            params.append(manager_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM manager_sessions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(0, limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._session(r) for r in rows]

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        self._reject_state_write(fields, "status", "manager_sessions")
        if "metadata" in fields:
            fields["metadata_json"] = _j(fields.pop("metadata"))
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE manager_sessions SET {cols} WHERE id=?",
                      (*fields.values(), session_id))

    @staticmethod
    def _session(r: sqlite3.Row) -> ManagerSession:
        return ManagerSession(
            id=r["id"], task_id=r["task_id"], review_id=r["review_id"],
            manager_id=r["manager_id"], workspace_id=r["workspace_id"],
            mode=r["mode"], status=r["status"], claim_id=r["claim_id"],
            started_at=r["started_at"], ended_at=r["ended_at"],
            launch_command=r["launch_command"], shell_command=r["shell_command"],
            metadata=_u(r["metadata_json"], {}),
            delegation_id=r["delegation_id"], parent_delegation_id=r["parent_delegation_id"],
        )

    # -------------------------------------------------- observation handles
    def upsert_observation_handle(
        self, entity_type: str, entity_id: str, handle: Any, task_id: Optional[str],
        state: str = "unknown", outcome: Optional[str] = None, at: float = 0.0,
    ) -> None:
        """Persist a provider's ObservationHandle so `agents attach|output|cancel`
        can reach the exact live pane later, keyed by (entity, provider)."""
        payload = handle.model_dump(mode="json") if hasattr(handle, "model_dump") else handle
        provider = payload.get("provider", "unknown")
        with self.transaction() as c:
            c.execute(
                "INSERT INTO observation_handles (id,entity_type,entity_id,task_id,provider,"
                "handle_json,state,outcome,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_type,entity_id,provider) DO UPDATE SET "
                "handle_json=excluded.handle_json,state=excluded.state,"
                "outcome=excluded.outcome,updated_at=excluded.updated_at,task_id=excluded.task_id",
                (f"obs_{entity_type}_{entity_id}", entity_type, entity_id, task_id, provider,
                 _j(payload), state, outcome, at, at),
            )

    def observation_handles_for(self, entity_type: str, entity_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observation_handles WHERE entity_type=? AND entity_id=?",
                (entity_type, entity_id),
            ).fetchall()
        return [self._obs_handle(r) for r in rows]

    def observation_handles_for_task(self, task_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observation_handles WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        return [self._obs_handle(r) for r in rows]

    def update_observation_handle_state(
        self, entity_type: str, entity_id: str, provider: str,
        state: str, outcome: Optional[str], at: float,
    ) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE observation_handles SET state=?,outcome=?,updated_at=? "
                "WHERE entity_type=? AND entity_id=? AND provider=?",
                (state, entity_type, entity_id, provider),
            )

    @staticmethod
    def _obs_handle(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "entity_type": r["entity_type"], "entity_id": r["entity_id"],
            "task_id": r["task_id"], "provider": r["provider"],
            "handle": _u(r["handle_json"], {}), "state": r["state"],
            "outcome": r["outcome"], "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    # ------------------------------------------------------- session tokens
    def insert_token(self, t: SessionToken, token_hash: str) -> SessionToken:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO session_tokens (id,session_id,token_hash,scope_json,expires_at,"
                "revoked_at,created_at) VALUES (?,?,?,?,?,?,?)",
                (t.id, t.session_id, token_hash, _j(t.scope), t.expires_at, t.revoked_at,
                 t.created_at),
            )
        return t

    def get_token_by_hash(self, token_hash: str) -> Optional[SessionToken]:
        """The only lookup there is. A plaintext token is never stored, so it can
        never be read back out — an attacker with the DB cannot impersonate."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
        return self._token(row) if row else None

    def revoke_tokens_for_session(self, session_id: str, at: float) -> int:
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE session_tokens SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                (at, session_id),
            )
        return cur.rowcount

    @staticmethod
    def _token(r: sqlite3.Row) -> SessionToken:
        return SessionToken(
            id=r["id"], session_id=r["session_id"], scope=_u(r["scope"], {}),
            expires_at=r["expires_at"], revoked_at=r["revoked_at"], created_at=r["created_at"],
        )

    # -------------------------------------------------------------- events
    def insert_event(self, e: Event) -> Event:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO events (id,task_id,kind,actor,payload_json,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (e.id, e.task_id, e.kind, e.actor, _j(e.payload), e.created_at),
            )
        return e

    # ------------------------------------------------- metrics / reconcile / backup
    def status_counts(self, table: str, column: str = "status") -> dict[str, int]:
        """`{status: count}` for a table. Table/column are internal constants, never
        user input, so the f-string interpolation carries no injection surface."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
            ).fetchall()
        return {str(r["k"]): int(r["n"]) for r in rows}

    def count_rows(self, table: str) -> int:
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def live_observation_handles(self) -> list[dict]:
        """Every handle not already in a terminal state, across all tasks. The
        reconcile pass probes these for liveness."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM observation_handles "
                "WHERE state NOT IN ('done','failed','cancelled') ORDER BY created_at"
            ).fetchall()
        return [self._obs_handle(r) for r in rows]

    def backup_to(self, dest_path: str) -> str:
        """Consistent online backup of the whole ledger via SQLite's backup API.

        Safe to call while the service is live and mid-write: the backup runs
        under the same connection lock and SQLite copies a transactionally
        consistent snapshot (it does not just `cp` the file, which could catch a
        half-written WAL). Returns the destination path.
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()
        return str(dest)

    def restore_from(self, src_path: str) -> str:
        """Restore the live ledger from a backup, in place, via the backup API.

        Copies ``src_path`` *into* the live connection under the lock, so open
        handles keep working and readers see the restored contents atomically —
        no file swap, no torn WAL. The source must be a SQLite database produced
        by :meth:`backup_to` (or any consistent copy of the ledger).
        """
        src = Path(src_path)
        if not src.exists():
            raise FileNotFoundError(f"backup not found: {src_path}")
        with self._lock:
            source = sqlite3.connect(str(src))
            try:
                source.backup(self._conn)
            finally:
                source.close()
            self._conn.commit()
        return str(src)

    def list_events(self, task_id: Optional[str] = None, limit: int = 100) -> list[Event]:
        with self._lock:
            if task_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
                    (task_id, max(0, limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (max(0, limit),)
                ).fetchall()
        return [
            Event(id=r["id"], task_id=r["task_id"], kind=r["kind"], actor=r["actor"],
                  payload=_u(r["payload_json"], {}), created_at=r["created_at"])
            for r in rows
        ]
