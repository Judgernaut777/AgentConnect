"""`GET /observe/tree` + `GET /observe` (docs/EVENT_BUS.md §8, Part 2 of the
ecosystem observability spec).

Three concerns: (1) the tree is correctly shaped from a seeded multi-level
run (session -> subtask -> run, delegation-wired, lease/tokens/cost/artifacts
populated); (2) privacy redaction holds at the BYTE level across every
surface a canary could leak through — `/observe/tree`, `/events`, the SSE
tail, and a direct scan of the `event_log` table itself; (3) the HTML page
serves publicly, carries no ledger data of its own, and references only
same-origin paths.
"""

from __future__ import annotations

import json

from conftest import operator_client  # noqa: E402

from agentconnect.api.routes_events import sse_lines
from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    PrivacyTier,
    SubtaskRequest,
    SubtaskStatus,
)


def _svc(tmp_path, **kwargs) -> AgentConnectService:
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "art"),
        workers=kwargs.pop("workers", [EchoWorker()]), **kwargs,
    )


def _find(node: dict, kind: str) -> list[dict]:
    out = [node] if node["kind"] == kind else []
    for child in node.get("children", []):
        out.extend(_find(child, kind))
    return out


# --------------------------------------------------------------- tree shape
def test_tree_shape_session_subtask_run_hierarchy_with_lease_tokens_cost(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    launched = svc.launch_session(manager_id="mgr-1", task_id=task.id, claim=True)
    session_id = launched["session"].id

    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="do the thing", instructions="please echo this", privacy_tier=PrivacyTier.public,
    ))
    assert sub.status is SubtaskStatus.succeeded

    tree = svc.observe_tree()
    assert tree["latest_seq"] == svc.latest_bus_seq()
    assert len(tree["roots"]) == 1
    root = tree["roots"][0]
    assert root["kind"] == "task" and root["id"] == task.id
    assert root["elapsed_s"] >= 0

    sessions = _find(root, "session")
    assert any(s["id"] == session_id for s in sessions)
    session_node = next(s for s in sessions if s["id"] == session_id)
    assert session_node["lease"] is not None
    assert session_node["lease"]["holder"] == "mgr-1"
    assert session_node["lease"]["expires_at"] is not None
    assert session_node["elapsed_s"] >= 0

    subtasks = _find(root, "subtask")
    assert len(subtasks) == 1
    sub_node = subtasks[0]
    assert sub_node["id"] == sub.id
    assert sub_node["state"] == "succeeded"
    assert sub_node["prompt"] == "please echo this"  # public tier: verbatim (redactor-scanned)
    assert sub_node["tokens"] is not None
    assert sub_node["tokens"]["input"] is not None and sub_node["tokens"]["output"] is not None
    assert sub_node["cost_usd"] == 0.0
    assert len(sub_node["artifacts"]) >= 1  # the echo worker's report + route explanation

    runs = _find(root, "run")
    assert len(runs) == 1
    run_node = runs[0]
    assert run_node["state"] == "succeeded"
    assert run_node["actor"] == "echo_worker"
    assert run_node["elapsed_s"] >= 0
    # the run is a child of the subtask, not a sibling
    assert run_node in sub_node["children"]


def test_terminal_tasks_excluded_unless_include_terminal(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))
    svc.cancel_task(task.id)

    default_tree = svc.observe_tree()
    assert task.id not in {r["id"] for r in default_tree["roots"]}

    with_terminal = svc.observe_tree(include_terminal=True)
    assert task.id in {r["id"] for r in with_terminal["roots"]}

    # An explicit task_id always returns it, terminal or not.
    scoped = svc.observe_tree(task_id=task.id)
    assert len(scoped["roots"]) == 1 and scoped["roots"][0]["id"] == task.id


def test_tree_assembly_issues_no_per_task_or_per_subtask_n_plus_one(tmp_path):
    """The all-tasks tree must read tasks in ONE query (no `get_task` per
    summary) and each task's runs in ONE query (no `list_runs` per subtask) —
    the two N+1 patterns the first implementation had."""
    svc = _svc(tmp_path)
    n_tasks, n_subtasks = 4, 3
    for t in range(n_tasks):
        task = svc.create_task(CreateTaskRequest(title=f"T{t}", created_by="human"))
        for s in range(n_subtasks):
            svc.submit_subtask(task.id, SubtaskRequest(title=f"s{s}", instructions="x"))

    statements: list[str] = []
    svc.storage._conn.set_trace_callback(statements.append)
    try:
        svc.observe_tree(include_terminal=True)
    finally:
        svc.storage._conn.set_trace_callback(None)

    task_selects = [s for s in statements
                    if "FROM tasks" in s and s.lstrip().upper().startswith("SELECT")]
    assert len(task_selects) == 1, task_selects
    run_selects = [s for s in statements
                   if "FROM worker_runs" in s and s.lstrip().upper().startswith("SELECT")]
    # one batched worker_runs read per TASK, never one per subtask
    assert len(run_selects) == n_tasks, run_selects


def test_unknown_task_id_is_not_found(tmp_path):
    from agentconnect.core.errors import NotFound

    svc = _svc(tmp_path)
    try:
        svc.observe_tree(task_id="task_does_not_exist")
        raise AssertionError("expected NotFound")
    except NotFound:
        pass


def test_current_tool_reflects_last_authorized_tool(tmp_path):
    from agentconnect.core.toolconnect_client import ToolDecision
    from agentconnect.core.workers import WorkerAdapter, WorkerCapabilities, WorkerResult

    class FakeGovernor:
        def authorize(self, principal, source_id, name, context=None, **kw):
            return ToolDecision(allowed=True, reason="ok", decision_id=f"dec-{name}",
                                determining_policies=(f"allow-{name}",), contract_version="1.1")

        def record(self, *a, **kw):
            return None

    class ToolWorker(WorkerAdapter):
        @property
        def worker_id(self) -> str:
            return "tool_worker"

        def capabilities(self) -> WorkerCapabilities:
            return WorkerCapabilities(
                worker_id="tool_worker", harness="demo", tools=["search", "write_artifact"],
                privacy_tiers=list(PrivacyTier), capability_tags=["echo"],
            )

        def run(self, subtask, context) -> WorkerResult:
            artifact = context.create_artifact(type="worker_output", content="ok", summary="ok")
            return WorkerResult(status="succeeded", summary="done",
                                artifacts=[{"artifact_id": artifact.id, "type": "worker_output"}])

    svc = _svc(tmp_path, workers=[ToolWorker()])
    svc.bind_tool_governor(FakeGovernor())
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(title="t", instructions="i"))
    assert sub.status is SubtaskStatus.succeeded

    node = _find(svc.observe_tree(task_id=task.id)["roots"][0], "subtask")[0]
    # the LAST declared tool authorized, per docs/EVENT_BUS.md §7 step 7
    assert node["current_tool"] == "write_artifact"


# ---------------------------------------------------------------- privacy
def test_secret_sensitive_never_serialized_anywhere_byte_level(tmp_path):
    canary = "CANARY_9f3_do_not_leak_this_instruction"
    # The TITLE carries its own canary on purpose: the bundled `EchoWorker`
    # echoes the subtask title verbatim into its result `summary`, so this
    # exercises the worker.completed/subtask.completed summary path too — the
    # leak the first implementation had (summary was not tier-gated).
    title_canary = "CANARY_SECRET_TITLE_e7a"
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title=title_canary, instructions=canary,
        privacy_tier=PrivacyTier.secret_sensitive,
    ))
    assert sub.status is SubtaskStatus.succeeded  # the echo worker really ran

    tree = svc.observe_tree(task_id=task.id)
    sub_node = _find(tree["roots"][0], "subtask")[0]
    assert sub_node["prompt"] == "[redacted: secret_sensitive]"
    assert canary not in json.dumps(tree)
    assert title_canary not in json.dumps(tree)

    client = operator_client(svc)
    tree_resp = client.get("/observe/tree", params={"task_id": task.id})
    assert canary not in tree_resp.text
    assert title_canary not in tree_resp.text

    events_resp = client.get("/events", params={"limit": 500})
    assert canary not in events_resp.text
    assert title_canary not in events_resp.text

    collected = []
    ticks = [0]

    def stop():
        ticks[0] += 1
        return len(collected) >= 30 or ticks[0] > 200

    for chunk in sse_lines(svc, 0, None, 0.0, should_stop=stop):
        collected.append(chunk)
    assert canary not in "".join(collected)
    assert title_canary not in "".join(collected)

    rows = svc.storage._conn.execute("SELECT payload_json FROM event_log").fetchall()
    blob = "".join(r["payload_json"] for r in rows)
    assert canary not in blob
    assert title_canary not in blob


def test_local_only_withheld_constant_no_length_signal(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    long_canary = "CANARY_local_" + ("x" * 5000)
    svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions=long_canary, privacy_tier=PrivacyTier.local_only,
    ))
    node = _find(svc.observe_tree(task_id=task.id)["roots"][0], "subtask")[0]
    assert node["prompt"] == "[withheld: local_only]"


def test_repo_sensitive_truncated_not_verbatim(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    long_text = "word " * 200  # well over 400 chars
    svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions=long_text, privacy_tier=PrivacyTier.repo_sensitive,
    ))
    node = _find(svc.observe_tree(task_id=task.id)["roots"][0], "subtask")[0]
    assert len(node["prompt"]) <= 420
    assert node["prompt"].endswith("…[truncated]")


def test_unknown_tier_fails_closed(tmp_path):
    """A corrupted/unrecognized privacy tier is treated as `secret_sensitive`
    (fail-closed), never passed through unredacted."""
    from agentconnect.core.observability.tree import _redact_text, _safe_tier

    assert _safe_tier("not-a-real-tier") is PrivacyTier.secret_sensitive
    out = _redact_text("CANARY", "not-a-real-tier", lambda t: (t, False))
    assert out == "[redacted: secret_sensitive]"


# -------------------------------------------------------------------- auth
def _manager_token(svc, task_id: str) -> str:
    return svc.launch_session("claude", task_id=task_id, claim=True)["token"]


def test_observe_tree_requires_operator_scope(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    mgr = _manager_token(svc, task.id)
    client = operator_client(svc)

    anon = client.__class__(client.app)
    assert anon.get("/observe/tree").status_code == 401

    manager_client = client.__class__(client.app)
    manager_client.headers.update({"Authorization": f"Bearer {mgr}"})
    assert manager_client.get("/observe/tree").status_code == 403

    resp = client.get("/observe/tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "roots" in body and "latest_seq" in body


# ---------------------------------------------------------------- HTML page
def test_observe_page_serves_tokenless_and_same_origin_only(tmp_path):
    import re

    svc = _svc(tmp_path)
    canary_title = "CANARY_page_must_not_contain_this_task_name"
    task = svc.create_task(CreateTaskRequest(title=canary_title, created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(title="d", instructions="x"))

    client = operator_client(svc)
    anon = client.__class__(client.app)
    resp = anon.get("/observe")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert canary_title not in body  # constant HTML; no ledger content baked in

    # every absolute-URL-shaped reference in the page must be same-origin
    # (a relative path, not http(s)://another-host); the page's only data
    # calls are to '/events' and '/observe/tree'.
    absolute_urls = re.findall(r"https?://[^\s'\"]+", body)
    assert absolute_urls == [], f"external references found: {absolute_urls}"
    assert "/observe/tree" in body
    assert "/events?since=" in body


def test_observe_page_declared_public_and_carries_no_ledger_data(tmp_path):
    from agentconnect.api.authz import PUBLIC_ROUTES

    assert ("GET", "/observe") in PUBLIC_ROUTES
