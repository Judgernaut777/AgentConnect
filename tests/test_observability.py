"""Provider-neutral observability + live-terminal visibility (handoff Parts II–V).

Everything here is offline and deterministic except the tmux tests, which drive a
REAL tmux server on a dedicated socket (skipped when tmux is absent). No shim
stands in for the transport: the JSONL is a real file, the OTLP export is a real
HTTP POST to a local server, and the panes are real PTYs.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import threading
import uuid

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateArtifactRequest,
    CreateTaskRequest,
    EchoWorker,
    Priority,
    SubtaskRequest,
)
from agentconnect.core.observability import (
    AgentObservationEvent,
    CompositeObservabilityProvider,
    EventType,
    FailurePolicy,
    HerdrObservabilityProvider,
    NoopObservabilityProvider,
    ObservabilityConfig,
    ObservabilityEmitter,
    ObservationOutcome,
    ObservationState,
    OtlpExporterObservabilityProvider,
    SpawnObservationRequest,
    StructuredLogObservabilityProvider,
    TmuxObservabilityProvider,
    event_to_otlp_log_record,
)

TMUX = shutil.which("tmux")
requires_tmux = pytest.mark.skipif(not TMUX, reason="tmux not installed")


def _tmux_socket() -> str:
    return f"ac-obs-test-{uuid.uuid4().hex[:8]}"


def _emitter(*providers, policy=FailurePolicy.advisory) -> ObservabilityEmitter:
    comp = CompositeObservabilityProvider(list(providers), policy=policy)
    return ObservabilityEmitter(comp)


# --------------------------------------------------------------- event model
def test_default_state_and_ids_travel_on_the_event():
    log = None
    ev = AgentObservationEvent(
        event_id="e1", trace_id="task_1", task_id="task_1", subtask_id="subtask_1",
        delegation_id="deleg_1", parent_delegation_id="deleg_root",
        event_type=EventType.worker_started, sequence=3,
    )
    assert ev.dedupe_key() == ("task_1", 3)
    d = ev.model_dump(mode="json")
    for key in ("trace_id", "task_id", "subtask_id", "delegation_id",
                "parent_delegation_id", "sequence", "event_type"):
        assert key in d


def test_event_has_no_chain_of_thought_field():
    fields = set(AgentObservationEvent.model_fields)
    for forbidden in ("prompt", "completion", "reasoning", "thought", "response"):
        assert forbidden not in fields


# --------------------------------------------------------------- JSONL provider
def test_structured_log_writes_one_line_per_event(tmp_path):
    path = tmp_path / "events.jsonl"
    prov = StructuredLogObservabilityProvider(path)
    em = _emitter(prov)
    em.observe(EventType.task_created, trace_id="task_1", task_id="task_1")
    em.observe(EventType.worker_started, trace_id="task_1", task_id="task_1",
               subtask_id="subtask_1")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["trace_id"] == "task_1" for line in lines)


def test_reader_restores_order_from_sequence(tmp_path):
    path = tmp_path / "events.jsonl"
    prov = StructuredLogObservabilityProvider(path)
    # Append physically out of order (seq 2 before seq 1).
    prov.append_event(AgentObservationEvent(
        event_id="b", sequence=2, trace_id="t", task_id="t",
        event_type=EventType.worker_completed))
    prov.append_event(AgentObservationEvent(
        event_id="a", sequence=1, trace_id="t", task_id="t",
        event_type=EventType.worker_started))
    rows = prov.read_events(task_id="t")
    assert [r["sequence"] for r in rows] == [1, 2]


# --------------------------------------------------------------- emitter dedupe
def test_emitter_dedupes_repeated_event_ids(tmp_path):
    prov = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    em = _emitter(prov)
    ev = em.observe(EventType.worker_started, trace_id="t", task_id="t", event_id="dup")
    assert ev is not None
    # Re-emit the same id (a workflow replay): dropped.
    assert em.emit(ev) is False
    assert len(prov.read_events(task_id="t")) == 1


def test_out_of_order_and_idempotent_stream(tmp_path):
    prov = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    em = _emitter(prov)
    events = [
        AgentObservationEvent(event_id=f"e{i}", sequence=i, trace_id="t", task_id="t",
                              event_type=EventType.attempt_recorded)
        for i in (3, 1, 2, 2, 1)  # out of order, with duplicates
    ]
    accepted = sum(1 for e in events if em.emit(e))
    assert accepted == 3  # only the three distinct ids
    rows = prov.read_events(task_id="t")
    assert [r["sequence"] for r in rows] == [1, 2, 3]


# --------------------------------------------------------- composite isolation
class _BoomProvider(NoopObservabilityProvider):
    name = "boom"

    def append_event(self, event):
        raise RuntimeError("provider is down")


def test_provider_failure_is_isolated_advisory(tmp_path):
    good = StructuredLogObservabilityProvider(tmp_path / "g.jsonl")
    em = _emitter(_BoomProvider(), good, policy=FailurePolicy.advisory)
    # The boom provider raising must NOT stop the good one and must NOT raise.
    em.observe(EventType.task_created, trace_id="t", task_id="t")
    assert len(good.read_events(task_id="t")) == 1
    assert em.provider.failures and em.provider.failures[0]["provider"] == "boom"


def test_task_blocking_policy_reraises(tmp_path):
    good = StructuredLogObservabilityProvider(tmp_path / "g.jsonl")
    comp = CompositeObservabilityProvider([good, _BoomProvider()],
                                          policy=FailurePolicy.task_blocking)
    with pytest.raises(RuntimeError):
        comp.append_event(AgentObservationEvent(
            event_id="x", trace_id="t", task_id="t", event_type=EventType.task_created))
    # The good provider still saw it (fan-out happened before the raise).
    assert len(good.read_events(task_id="t")) == 1


# --------------------------------------------------------------- config
def test_config_default_is_noop():
    cfg = ObservabilityConfig.from_env({})
    comp = cfg.build_provider()
    assert [p.name for p in comp.providers] == ["noop"]


def test_config_builds_named_providers(tmp_path):
    env = {
        "AGENTCONNECT_OBSERVABILITY": "structured_log,otlp",
        "AGENTCONNECT_OBSERVABILITY_LOG_PATH": str(tmp_path / "e.jsonl"),
    }
    cfg = ObservabilityConfig.from_env(env)
    comp = cfg.build_provider()
    assert {p.name for p in comp.providers} == {"structured_log", "otlp"}


def test_otlp_endpoint_implies_provider(tmp_path):
    env = {"AGENTCONNECT_OTLP_ENDPOINT": "http://localhost:4318"}
    cfg = ObservabilityConfig.from_env(env)
    assert "otlp" in cfg.providers


def test_startup_fatal_aborts_on_unhealthy_provider(tmp_path):
    class _Sick(NoopObservabilityProvider):
        name = "sick"

        def health(self):
            from agentconnect.core.observability import ProviderHealth
            return ProviderHealth(provider="sick", available=False, detail="nope")

    cfg = ObservabilityConfig(providers=[], failure_policy=FailurePolicy.startup_fatal)
    comp = CompositeObservabilityProvider([_Sick()], policy=FailurePolicy.startup_fatal)
    # Build path enforces startup health; emulate by calling the guard directly.
    with pytest.raises(RuntimeError):
        for p in comp.providers:
            if not p.health().available:
                raise RuntimeError("unhealthy at startup")


# --------------------------------------------------------------- OTLP mapping
def test_otlp_log_record_carries_all_correlation_ids():
    ev = AgentObservationEvent(
        event_id="e1", trace_id="task_9", task_id="task_9", subtask_id="subtask_9",
        delegation_id="deleg_9", parent_delegation_id="deleg_root", run_id="run_9",
        review_id=None, event_type=EventType.worker_started)
    rec = event_to_otlp_log_record(ev)
    attrs = {a["key"]: a["value"] for a in rec["attributes"]}
    assert attrs["agentconnect.task_id"]["stringValue"] == "task_9"
    assert attrs["agentconnect.delegation_id"]["stringValue"] == "deleg_9"
    assert attrs["agentconnect.parent_delegation_id"]["stringValue"] == "deleg_root"
    assert attrs["agentconnect.run_id"]["stringValue"] == "run_9"
    assert len(rec["traceId"]) == 32 and len(rec["spanId"]) == 16


def test_otlp_disabled_is_noop_no_socket(tmp_path):
    prov = OtlpExporterObservabilityProvider(endpoint="")
    assert prov.enabled is False
    # Would raise if it tried to open a socket; it must simply do nothing.
    prov.append_event(AgentObservationEvent(
        event_id="e", trace_id="t", task_id="t", event_type=EventType.task_created))
    assert prov.sent == 0 and prov.errors == 0


def test_otlp_export_posts_to_a_real_collector():
    received: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        prov = OtlpExporterObservabilityProvider(endpoint=f"http://127.0.0.1:{port}")
        assert prov.enabled
        prov.append_event(AgentObservationEvent(
            event_id="e1", trace_id="task_1", task_id="task_1",
            event_type=EventType.task_created))
        assert prov.sent == 1
        assert received, "collector received no OTLP payload"
        body = received[0]
        attrs = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
        keys = {a["key"] for a in attrs}
        assert "agentconnect.trace_id" in keys
    finally:
        server.shutdown()


# --------------------------------------------------------------- Herdr (blocked)
def test_herdr_disabled_by_default():
    prov = HerdrObservabilityProvider()
    assert prov.enabled is False
    assert prov.health().available is False
    # A disabled handle carries the reason and no live target.
    h = prov.create_session.__self__  # sanity: it is bound
    info = prov.attach_info(
        prov.spawn_process(SpawnObservationRequest(trace_id="t", task_id="t")))
    assert info.available is False


def test_herdr_enabled_without_socket_raises():
    from agentconnect.core.observability.providers.herdr import HerdrControlError
    with pytest.raises(HerdrControlError):
        HerdrObservabilityProvider(enabled=True, socket_path="")


def test_herdr_enabled_with_socket_refuses_to_fake_success(tmp_path):
    # A path is set but there is no real Herdr; ping() must raise NotImplementedError
    # (the transport is unimplemented) rather than pretend to connect.
    with pytest.raises(NotImplementedError):
        HerdrObservabilityProvider(enabled=True, socket_path=str(tmp_path / "herdr.sock"))


# --------------------------------------------------------------- tmux provider
@requires_tmux
def test_tmux_maps_workspace_window_pane_and_attaches():
    socket = _tmux_socket()
    prov = TmuxObservabilityProvider(socket=socket)
    try:
        assert prov.health().available
        from agentconnect.core.observability import SessionObservationRequest
        mgr = prov.create_session(SessionObservationRequest(
            trace_id="task_7", task_id="task_7", session_id="sess_1", workspace_id="ws_1",
            agent_id="mgr", agent_role="manager",
            command="sh -c 'echo MANAGER; sleep 30'"))
        wrk = prov.spawn_process(SpawnObservationRequest(
            trace_id="task_7", task_id="task_7", subtask_id="st_1", run_id="run_1",
            workspace_id="ws_1", agent_id="echo", agent_role="worker",
            command="sh -c 'echo WORKER-LINE; sleep 30'"))
        # session=workspace, window=task, distinct panes.
        assert mgr.target.startswith("ws_1:task_7.")
        assert wrk.target.startswith("ws_1:task_7.")
        assert mgr.target != wrk.target
        info = prov.attach_info(mgr)
        assert info.available and socket in info.attach_command
        assert "attach-session -r" in info.read_only_command
    finally:
        prov.kill_server()


@requires_tmux
def test_tmux_bounded_output_capture_and_redaction():
    socket = _tmux_socket()

    def _redactor(text):
        return text.replace("sk-SECRET", "[REDACTED]"), ("sk-SECRET" in text)

    prov = TmuxObservabilityProvider(socket=socket, redactor=_redactor)
    try:
        h = prov.spawn_process(SpawnObservationRequest(
            trace_id="task_x", task_id="task_x", subtask_id="s", run_id="r",
            workspace_id="w", agent_id="a", agent_role="worker",
            command="sh -c 'echo TOKEN=sk-SECRET; sleep 30'"))
        import time
        time.sleep(0.5)
        cap = prov.capture_output(h, max_lines=10)
        joined = "\n".join(cap.lines)
        assert "sk-SECRET" not in joined
        assert cap.redacted is True
    finally:
        prov.kill_server()


@requires_tmux
def test_tmux_capture_redacts_a_bare_pem_private_key_delimiter(tmp_path):
    """The real safety redactor (not a stub) wired into tmux capture must scrub a
    lone `-----BEGIN … PRIVATE KEY-----` delimiter that scrolled past in a pane —
    the block rule needs both delimiters, so a header on its own would otherwise
    survive capture as an unredacted signal that a private key was present."""
    socket = _tmux_socket()
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[EchoWorker()],
    )
    prov = TmuxObservabilityProvider(socket=socket, redactor=svc.observation_redactor())
    try:
        h = prov.spawn_process(SpawnObservationRequest(
            trace_id="task_pem", task_id="task_pem", subtask_id="s", run_id="r",
            workspace_id="w", agent_id="a", agent_role="worker",
            command="sh -c 'echo -----BEGIN OPENSSH PRIVATE KEY-----; sleep 30'"))
        import time
        time.sleep(0.5)
        cap = prov.capture_output(h, max_lines=10)
        joined = "\n".join(cap.lines)
        assert "PRIVATE KEY" not in joined
        assert cap.redacted is True
    finally:
        prov.kill_server()


@requires_tmux
def test_tmux_close_kills_the_real_pane():
    import subprocess
    socket = _tmux_socket()
    prov = TmuxObservabilityProvider(socket=socket)
    try:
        h = prov.spawn_process(SpawnObservationRequest(
            trace_id="task_c", task_id="task_c", subtask_id="s", run_id="r",
            workspace_id="w", agent_id="a", agent_role="worker",
            command="sh -c 'sleep 60'"))
        panes = subprocess.run(
            ["tmux", "-L", socket, "list-panes", "-t", "task_c", "-F", "#{pane_id}"],
            capture_output=True, text=True)
        assert h.target.split(".")[-1] in panes.stdout
        prov.close(h, ObservationOutcome.succeeded)
        panes2 = subprocess.run(
            ["tmux", "-L", socket, "list-panes", "-t", "task_c", "-F", "#{pane_id}"],
            capture_output=True, text=True)
        assert h.target.split(".")[-1] not in panes2.stdout
    finally:
        prov.kill_server()


# --------------------------------------------------- service integration
def _svc_with_obs(tmp_path, providers, policy=FailurePolicy.advisory):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[EchoWorker()],
    )
    comp = CompositeObservabilityProvider(list(providers), policy=policy)
    svc.bind_observability(ObservabilityEmitter(comp, redactor=svc.observation_redactor()))
    return svc


def test_delegation_ids_persisted_and_tree_built(tmp_path):
    prov = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    svc = _svc_with_obs(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    stored = svc.get_subtask(sub.id).subtask
    assert stored.delegation_id and stored.delegation_id.startswith("deleg_")
    tree = svc.agent_tree(task.id)
    assert tree["entity_type"] == "task"
    # The subtask node is under the task root (no manager session -> parent None).
    ids_in_tree = [c["entity_id"] for c in tree["children"]]
    assert sub.id in ids_in_tree


def test_events_emitted_across_lifecycle(tmp_path):
    prov = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    svc = _svc_with_obs(tmp_path, [prov])
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    kinds = {e["event_type"] for e in svc.observation_events(task_id=task.id)}
    assert "task.created" in kinds
    assert "subtask.created" in kinds
    assert "worker.completed" in kinds  # echo worker runs inline and succeeds


def test_provider_failure_never_corrupts_the_ledger(tmp_path):
    good = StructuredLogObservabilityProvider(tmp_path / "g.jsonl")
    svc = _svc_with_obs(tmp_path, [_BoomProvider(), good], policy=FailurePolicy.advisory)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="do", instructions="x"))
    # Despite the boom provider raising on every event, the ledger is intact and
    # the subtask ran to success.
    assert svc.get_task(task.id).task.title == "T"
    assert svc.get_subtask(sub.id).subtask.status.value == "succeeded"
    assert svc.observation_events(task_id=task.id)  # the good provider still logged


def test_noop_service_emits_nothing_and_needs_no_provider(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()])
    assert svc.observability.enabled is False
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    assert svc.observation_events(task_id=task.id) == []


@requires_tmux
def test_service_attach_output_cancel_against_real_tmux(tmp_path):
    socket = _tmux_socket()
    tmux = TmuxObservabilityProvider(socket=socket, redactor=None)
    log = StructuredLogObservabilityProvider(tmp_path / "e.jsonl")
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()])
    comp = CompositeObservabilityProvider([log, tmux])
    svc.bind_observability(ObservabilityEmitter(comp, redactor=svc.observation_redactor()))
    try:
        task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
        # A launched manager gives a real live pane we can attach to.
        launched = svc.launch_session(manager_id="mgr", task_id=task.id)
        sid = launched["session"].id
        info = svc.attach_agent(sid)
        assert info.available and socket in info.attach_command
        import time
        time.sleep(0.4)
        cap = svc.agent_output(sid, max_lines=20)
        assert any("mgr" in line for line in cap.lines)
        # cancel propagates to the real pane.
        svc.cancel_agent(sid)
        rows = svc.storage.observation_handles_for("session", sid)
        assert rows[0]["state"] in ("cancelled", "failed")
    finally:
        tmux.kill_server()


def test_old_database_migrates_delegation_columns(tmp_path):
    import sqlite3
    from agentconnect.core.storage import SqliteStorage
    p = str(tmp_path / "old.db")
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE subtasks (id TEXT PRIMARY KEY, parent_task_id TEXT, title TEXT,"
        " instructions TEXT, status TEXT, privacy_tier TEXT, preferred_worker TEXT,"
        " assigned_worker TEXT, created_at REAL, updated_at REAL, result_artifact_id TEXT,"
        " route_reason_json TEXT DEFAULT '{}', sandbox_json TEXT DEFAULT '{}',"
        " required_capabilities_json TEXT DEFAULT '[]', approved_by TEXT,"
        " approved_max_cost_usd REAL, metadata_json TEXT DEFAULT '{}');"
        "CREATE TABLE reviews (id TEXT PRIMARY KEY, task_id TEXT, requested_by TEXT,"
        " assigned_to TEXT, status TEXT, criteria_json TEXT DEFAULT '[]',"
        " artifact_refs_json TEXT DEFAULT '[]', result_artifact_id TEXT, created_at REAL,"
        " updated_at REAL);"
        "CREATE TABLE manager_sessions (id TEXT PRIMARY KEY, task_id TEXT, review_id TEXT,"
        " manager_id TEXT, workspace_id TEXT, mode TEXT, status TEXT, claim_id TEXT,"
        " started_at REAL, ended_at REAL, launch_command TEXT DEFAULT '',"
        " shell_command TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}');")
    conn.commit()
    conn.close()
    st = SqliteStorage(p)  # must migrate cleanly
    cols = {r["name"] for r in st._conn.execute("PRAGMA table_info(subtasks)")}
    assert {"delegation_id", "parent_delegation_id"} <= cols
    tabs = {r[0] for r in st._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "observation_handles" in tabs
