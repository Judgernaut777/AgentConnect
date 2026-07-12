"""Production-readiness: orphan reconcile, ops metrics/readiness, backup/restore,
retry idempotency, concurrency, restart durability, and event redaction.

Everything here is offline and deterministic except the tmux tests, which drive a
REAL tmux server on a dedicated socket (skipped when tmux is absent). No shim
stands in for a transport: reconcile-by-liveness against tmux kills a real pane's
real process and asserts the ledger heals; backup/restore round-trips a real
SQLite file on disk; restart durability closes and re-opens a real DB file.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import uuid

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    SubtaskRequest,
)
from agentconnect.core.observability import (
    CompositeObservabilityProvider,
    ObservabilityEmitter,
    ObservationHandle,
    ObservationOutcome,
    SessionObservationRequest,
    SpawnObservationRequest,
    StructuredLogObservabilityProvider,
    TmuxObservabilityProvider,
)
from agentconnect.core.observability.provider import AgentObservabilityProvider

TMUX = shutil.which("tmux")
requires_tmux = pytest.mark.skipif(not TMUX, reason="tmux not installed")


def _tmux_socket() -> str:
    return f"ac-recon-{uuid.uuid4().hex[:8]}"


class _ControllableProvider(AgentObservabilityProvider):
    """A provider that hands back a real handle and answers `is_live` from a dict
    the test controls, so reconcile-by-liveness is exercised deterministically."""

    name = "controllable"

    def __init__(self) -> None:
        self.alive: dict[str, bool | None] = {}

    def create_session(self, request: SessionObservationRequest) -> ObservationHandle:
        target = f"pane-{request.session_id}"
        self.alive.setdefault(target, True)
        return ObservationHandle(provider=self.name, handle_id=target, kind="session",
                                 target=target, task_id=request.task_id)

    def spawn_process(self, request: SpawnObservationRequest) -> ObservationHandle:
        target = f"pane-{request.run_id or request.subtask_id}"
        self.alive.setdefault(target, True)
        return ObservationHandle(provider=self.name, handle_id=target, kind="process",
                                 target=target, task_id=request.task_id)

    def is_live(self, handle: ObservationHandle):
        return self.alive.get(handle.target)


def _svc(tmp_path, providers):
    svc = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "art"),
        workers=[EchoWorker()],
    )
    comp = CompositeObservabilityProvider(list(providers))
    svc.bind_observability(ObservabilityEmitter(comp, redactor=svc.observation_redactor()))
    return svc


# --------------------------------------------------------- reconcile: liveness
def test_reconcile_by_liveness_sweeps_dead_session(tmp_path):
    prov = _ControllableProvider()
    svc = _svc(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr", task_id=task.id)
    sid = launched["session"].id
    assert svc.get_session(sid).status.value in ("prepared", "running")

    # The process dies with no terminal event (a crash).
    prov.alive[f"pane-{sid}"] = False
    report = svc.reconcile_orphans()

    assert [e["session_id"] for e in report["reconciled_sessions"]] == [sid]
    assert report["reconciled_sessions"][0]["detected_by"] == "liveness"
    session = svc.get_session(sid)
    assert session.status.value == "abandoned"
    assert session.metadata["reconciled"]["reason"]
    # Token revoked as part of reconcile.
    assert svc.revoke_session_tokens(sid) == 0  # already none active


def test_live_session_is_never_reconciled(tmp_path):
    prov = _ControllableProvider()
    svc = _svc(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr", task_id=task.id)
    sid = launched["session"].id
    # Provider proves it is alive -> reconcile leaves it alone even with age=0.
    report = svc.reconcile_orphans(older_than_seconds=0)
    assert report["reconciled_sessions"] == []
    assert svc.get_session(sid).status.value in ("prepared", "running")


def test_unknown_liveness_is_not_a_dead_verdict(tmp_path):
    # A provider that cannot tell (is_live -> None) must NOT cause reconciliation
    # from liveness alone; only the age gate can then act.
    prov = _ControllableProvider()
    svc = _svc(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr", task_id=task.id)
    sid = launched["session"].id
    prov.alive[f"pane-{sid}"] = None  # unknown
    assert svc.reconcile_orphans()["reconciled_sessions"] == []  # no age gate -> nothing
    # With an age gate it reconciles (heartbeat timeout), detected_by=age.
    report = svc.reconcile_orphans(older_than_seconds=0)
    assert report["reconciled_sessions"][0]["detected_by"] == "age"


# --------------------------------------------------------- reconcile: age gate
def test_reconcile_by_age_without_a_live_provider(tmp_path):
    # JSONL provider has no liveness surface (is_live -> None); the age gate is the
    # only signal, exactly the fallback a deployment without tmux relies on.
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr", task_id=task.id)
    sid = launched["session"].id
    # No age gate -> nothing (we never reconcile a session we cannot prove is dead).
    assert svc.reconcile_orphans()["reconciled_sessions"] == []
    # Age gate of 0 -> everything older than now is swept.
    report = svc.reconcile_orphans(older_than_seconds=0)
    assert sid in [e["session_id"] for e in report["reconciled_sessions"]]
    assert svc.get_session(sid).status.value == "abandoned"


def test_reconcile_is_idempotent(tmp_path):
    prov = _ControllableProvider()
    svc = _svc(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sid = svc.launch_session(manager_id="mgr", task_id=task.id)["session"].id
    prov.alive[f"pane-{sid}"] = False
    first = svc.reconcile_orphans()
    assert first["reconciled_sessions"]
    second = svc.reconcile_orphans()
    assert second["reconciled_sessions"] == []  # nothing left to do


def test_reconcile_dry_run_mutates_nothing(tmp_path):
    prov = _ControllableProvider()
    svc = _svc(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sid = svc.launch_session(manager_id="mgr", task_id=task.id)["session"].id
    prov.alive[f"pane-{sid}"] = False
    report = svc.reconcile_orphans(dry_run=True)
    assert report["dry_run"] and report["reconciled_sessions"]
    # The session is still live: dry-run only reports.
    assert svc.get_session(sid).status.value in ("prepared", "running")
    # A real pass then actually sweeps it.
    assert svc.reconcile_orphans()["reconciled_sessions"]
    assert svc.get_session(sid).status.value == "abandoned"


@requires_tmux
def test_tmux_is_live_true_then_false_on_real_pane(tmp_path):
    socket = _tmux_socket()
    prov = TmuxObservabilityProvider(socket=socket)
    try:
        # An idle pane is alive.
        alive = prov.spawn_process(SpawnObservationRequest(
            trace_id="t", task_id="t", subtask_id="s", run_id="r",
            agent_id="a", agent_role="worker",
            command="sh -c 'while :; do sleep 3600; done'"))
        assert prov.is_live(alive) is True
        # A pane whose command exits becomes dead (remain-on-exit keeps it readable).
        dead = prov.spawn_process(SpawnObservationRequest(
            trace_id="t", task_id="t", subtask_id="s2", run_id="r2",
            agent_id="a", agent_role="worker", command="true"))
        import time
        for _ in range(50):
            if prov.is_live(dead) is False:
                break
            time.sleep(0.1)
        assert prov.is_live(dead) is False
    finally:
        prov.kill_server()


@requires_tmux
def test_reconcile_against_real_tmux_end_to_end(tmp_path):
    socket = _tmux_socket()
    tmux = TmuxObservabilityProvider(socket=socket)
    svc = AgentConnectService.create(
        db_path=str(tmp_path / "l.db"), artifact_dir=str(tmp_path / "a"),
        workers=[EchoWorker()])
    comp = CompositeObservabilityProvider(
        [StructuredLogObservabilityProvider(tmp_path / "e.jsonl"), tmux])
    svc.bind_observability(ObservabilityEmitter(comp, redactor=svc.observation_redactor()))
    try:
        task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
        sid = svc.launch_session(manager_id="mgr", task_id=task.id)["session"].id
        rows = svc.storage.observation_handles_for("session", sid)
        row = next(r for r in rows if r["provider"] == "tmux")
        target = row["handle"]["target"]
        assert target
        # Kill the pane's real process to simulate a crashed agent (no end_shell).
        pid = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "-t", target,
             "-F", "#{pane_pid}"], capture_output=True, text=True).stdout.strip()
        subprocess.run(["kill", "-9", pid], check=False)
        import time
        for _ in range(50):
            if svc.reconcile_orphans()["reconciled_sessions"]:
                break
            time.sleep(0.1)
        assert svc.get_session(sid).status.value == "abandoned"
    finally:
        tmux.kill_server()


# ------------------------------------------------------------------- metrics
def test_metrics_report_status_counts(tmp_path):
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    m = svc.metrics()
    assert m["totals"]["tasks"] == 1
    assert sum(m["subtasks"].values()) == 1
    assert m["totals"]["runs"] >= 1  # echo worker ran
    assert "succeeded" in m["runs"]
    assert m["observability"]["enabled"] is True


def test_readiness_ok_and_degraded(tmp_path):
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    assert svc.readiness()["ready"] is True
    # Break storage: readiness must fail closed.
    svc.storage.close()
    report = svc.readiness()
    assert report["ready"] is False
    assert report["checks"]["storage"]["ok"] is False


# ---------------------------------------------------------------- backup/restore
def test_backup_and_restore_round_trip(tmp_path):
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    task = svc.create_task(CreateTaskRequest(title="Original", created_by="human"))
    backup_path = str(tmp_path / "backup.db")
    info = svc.backup_ledger(backup_path)
    assert info["size_bytes"] > 0 and info["tasks"] == 1

    # Mutate AFTER the backup: create a second task.
    svc.create_task(CreateTaskRequest(title="AfterBackup", created_by="human"))
    assert svc.storage.count_rows("tasks") == 2

    # Restore rolls the ledger back to the snapshot: the second task is gone.
    svc.restore_ledger(backup_path)
    assert svc.storage.count_rows("tasks") == 1
    assert svc.get_task(task.id).task.title == "Original"


def test_restore_rejects_missing_file(tmp_path):
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    with pytest.raises(FileNotFoundError):
        svc.restore_ledger(str(tmp_path / "nope.db"))


# ---------------------------------------------------------- retry idempotency
def test_run_subtask_retry_does_not_duplicate(tmp_path):
    # A retried activity (lost heartbeat, re-delivered task) must not double-run a
    # worker or duplicate its artifacts/attempts.
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    # The echo worker already ran to completion inline on submit.
    runs_before = svc.storage.count_rows("worker_runs")
    arts_before = svc.storage.count_rows("artifacts")
    # Re-invoke run_subtask twice: it must be a no-op on a terminal subtask.
    svc.run_subtask(sub.id)
    svc.run_subtask(sub.id)
    assert svc.storage.count_rows("worker_runs") == runs_before
    assert svc.storage.count_rows("artifacts") == arts_before
    assert svc.get_subtask(sub.id).subtask.status.value == "succeeded"


# ----------------------------------------------------------------- concurrency
def test_concurrent_subtasks_do_not_overwrite_each_other(tmp_path):
    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    created: list[str] = []
    lock = threading.Lock()
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            sub = svc.submit_subtask(
                task.id, SubtaskRequest(title=f"s{i}", instructions="x"))
            with lock:
                created.append(sub.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # Every subtask persisted, each distinct, none clobbered.
    assert len(set(created)) == 16
    stored = {s.id for s in svc.storage.list_subtasks(task.id)}
    assert set(created) == stored
    assert svc.storage.count_rows("subtasks") == 16


# ------------------------------------------------------------ restart durability
def test_restart_preserves_session_and_run_history(tmp_path):
    from agentconnect.core.storage import SqliteStorage

    db = str(tmp_path / "durable.db")
    svc = AgentConnectService.create(
        db_path=db, artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()])
    task = svc.create_task(CreateTaskRequest(title="Durable", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    sid = svc.launch_session(manager_id="mgr", task_id=task.id)["session"].id
    tasks_before = svc.storage.count_rows("tasks")
    runs_before = svc.storage.count_rows("worker_runs")

    # "Kill" the service: drop the process/connection entirely.
    svc.storage.close()
    del svc

    # "Restart": a brand-new service over the same file. History intact.
    svc2 = AgentConnectService.create(
        db_path=db, artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()])
    assert svc2.storage.count_rows("tasks") == tasks_before
    assert svc2.storage.count_rows("worker_runs") == runs_before
    assert svc2.get_task(task.id).task.title == "Durable"
    assert svc2.get_session(sid).manager_id == "mgr"
    assert svc2.get_subtask(sub.id).subtask.status.value == "succeeded"


# ---------------------------------------------------------- rc1 upgrade path
def test_rc1_schema_upgrades_and_keeps_rows(tmp_path):
    """A ledger created by v0.1.0-rc1 (pre-observability schema) upgrades forward
    on open: new columns/tables are added and every existing row survives."""
    import sqlite3

    from agentconnect.core.storage import SqliteStorage

    p = str(tmp_path / "rc1.db")
    conn = sqlite3.connect(p)
    # rc1-era subset: tasks + a session row, WITHOUT delegation columns or the
    # observation_handles table (both introduced after rc1).
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT "
        "NOT NULL DEFAULT '', status TEXT NOT NULL, priority TEXT NOT NULL, "
        "created_by TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
        "current_manager TEXT, handoff_summary TEXT, linear_issue_id TEXT, "
        "linear_issue_url TEXT, metadata_json TEXT NOT NULL DEFAULT '{}');"
        "CREATE TABLE manager_sessions (id TEXT PRIMARY KEY, task_id TEXT, review_id "
        "TEXT, manager_id TEXT, workspace_id TEXT, mode TEXT, status TEXT, claim_id "
        "TEXT, started_at REAL, ended_at REAL, launch_command TEXT DEFAULT '', "
        "shell_command TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}');")
    conn.execute(
        "INSERT INTO tasks (id,title,goal,status,priority,created_by,created_at,"
        "updated_at) VALUES ('task_rc1','Legacy','g','queued','normal','human',1,1)")
    conn.execute(
        "INSERT INTO manager_sessions (id,task_id,manager_id,mode,status,started_at) "
        "VALUES ('sess_rc1','task_rc1','mgr','manager','running',1)")
    conn.commit()
    conn.close()

    st = SqliteStorage(p)  # migrates on open
    cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(manager_sessions)")}
    assert {"delegation_id", "parent_delegation_id"} <= cols
    tabs = {r[0] for r in st._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "observation_handles" in tabs
    # The legacy rows survived the upgrade untouched.
    assert st.get_task("task_rc1").title == "Legacy"
    assert st.get_session("sess_rc1").manager_id == "mgr"
    # And the service can now use the reconcile/metrics surface on the upgraded DB.
    svc = AgentConnectService.create(db_path=p, artifact_dir=str(tmp_path / "a"))
    assert svc.metrics()["totals"]["tasks"] == 1
    assert svc.reconcile_orphans(older_than_seconds=0)["reconciled_sessions"]


# ------------------------------------------------ security: managed-agent refusal
def test_managed_agent_cannot_run_operator_ledger_ops(monkeypatch):
    """backup/restore/reconcile are operator actions; the CLI refuses them inside a
    managed-agent session (AGENTCONNECT_MODE set), the same guard that already
    blocks self-completion and memory promotion."""
    from agentconnect.cli.main import _refuse_operator_command, build_parser

    monkeypatch.setenv("AGENTCONNECT_MODE", "manager")
    for argv in (["backup", "/tmp/x.db"],
                 ["restore", "/tmp/x.db", "--yes"],
                 ["sessions", "reconcile"]):
        args = build_parser().parse_args(argv)
        refusal = _refuse_operator_command(args)
        assert refusal and "operator action" in refusal, argv

    # As the operator (no AGENTCONNECT_MODE) the same commands are allowed.
    monkeypatch.delenv("AGENTCONNECT_MODE", raising=False)
    for argv in (["backup", "/tmp/x.db"], ["sessions", "reconcile"]):
        assert _refuse_operator_command(build_parser().parse_args(argv)) is None


# --------------------------------------------------------- HTTP ops surface
def test_http_health_ready_and_metrics(tmp_path):
    from fastapi.testclient import TestClient

    from agentconnect.api.app import create_app
    from conftest import operator_client

    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))

    anon = TestClient(create_app(service=svc, linear_sync=None))
    # Liveness and readiness are unauthenticated probes.
    assert anon.get("/health").status_code == 200
    ready = anon.get("/ready")
    assert ready.status_code == 200 and ready.json()["ready"] is True
    # Metrics is authenticated: no token -> 401 (not a public probe).
    assert anon.get("/metrics").status_code == 401

    op = operator_client(svc)
    body = op.get("/metrics").json()
    assert body["totals"]["tasks"] == 1
    assert "runs" in body and "sessions" in body


def test_http_ready_returns_503_when_storage_is_down(tmp_path):
    from fastapi.testclient import TestClient

    from agentconnect.api.app import create_app

    svc = _svc(tmp_path, [StructuredLogObservabilityProvider(tmp_path / "e.jsonl")])
    anon = TestClient(create_app(service=svc, linear_sync=None))
    svc.storage.close()  # hard-dependency failure
    resp = anon.get("/ready")
    assert resp.status_code == 503 and resp.json()["ready"] is False


# ------------------------------------------------------------- event redaction
def test_event_metadata_redacts_sensitive_fields(tmp_path):
    log = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    svc = _svc(tmp_path, [log])
    # Enable the safety layer so the string redactor is active.
    from agentconnect.core.observability.emitter import ObservabilityEmitter

    emitter = ObservabilityEmitter(
        CompositeObservabilityProvider([log]),
        redactor=lambda t: (t.replace("sk-secret-123", "[REDACTED]"),
                            "sk-secret-123" in t),
    )
    redacted = emitter._redact_metadata({
        "token": "abc123",                     # sensitive key -> masked
        "api_key": "xyz",                      # sensitive key -> masked
        "note": "the key is sk-secret-123",    # redactor scrubs the value
        "count": 7,                            # scalar passes through
    })
    assert redacted["token"] == "[redacted]"
    assert redacted["api_key"] == "[redacted]"
    assert "sk-secret-123" not in redacted["note"] and "[REDACTED]" in redacted["note"]
    assert redacted["count"] == 7
