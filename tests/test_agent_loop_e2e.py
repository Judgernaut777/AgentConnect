"""The proprietary-agent operational loop, end to end.

The memory boundary is stable. The open question is different: **does
`launch` / `shell` / `audit` actually force durable work through AgentConnect?**

So nothing here is mocked at the seam that matters. The manager and the reviewer
are real subprocesses, launched by the real `agentconnect shell`, that speak to
the ledger only through the tools that survive environment sanitization. They are
given no database handle, no backend credentials, and no arguments — everything
they need arrives in the environment, the way a real Claude Code or Codex session
receives it.

The one thing that *is* a double is WikiBrain. `_WikiBrainHandler` is a real HTTP
server on a real port — so the adapter's httpx path runs for once, rather than a
transport double — but it serves canned responses and never touches WikiBrain. Its
job is to prove the wire works, not the ledger. Real WikiBrain semantics are pinned
in `test_wikibrain_integration.py`, which drives `wiki.api` in-process through a
transport shim. Neither test has both halves at once: nothing here exercises real
WikiBrain over real HTTP, and nothing will until WikiBrain ships `wiki serve`.

The twelve steps of the loop, in order:

  1. `agentconnect launch <agent> --task <id> --claim`
  2. `agentconnect shell --task <id> -- <agent>`
  3. the agent retrieves its context pack
  4. it records an attempt
  5. it submits a subtask
  6. the worker_brief is injected into `subtask.metadata["context_pack"]`
  7. the worker produces an artifact
  8. the manager requests review
  9. the reviewer gets a `reviewer_brief`
 10. the review completes
 11. the audit passes
 12. completion updates AgentConnect, and *then* Linear

Steps 11 and 12 are operator actions, run in the parent process. That is the
point: an agent's token buys none of them.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    SessionStatus,
    TaskStatus,
)
from agentconnect.core.errors import PolicyViolation, Unauthenticated

REPO = Path(__file__).resolve().parents[1]
SRC = [str(REPO / "packages" / p / "src") for p in (
    "agentconnect-cli", "agentconnect-core", "agentconnect-linear",
    "agentconnect-mcp", "agentconnect-temporal")]

TRUSTED_CLAIM = "Refresh token validation lives in auth/session.py."
CONSTRAINT = "No schema changes"


# ------------------------------------------------------- a real WikiBrain server
class _WikiBrainHandler(BaseHTTPRequestHandler):
    captured: list = []

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send({"backend": "wikibrain", "status": "ok"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/recall"):
            # `trusted` is the authority's verdict, and the only authority signal.
            self._send({"items": [{
                "text": TRUSTED_CLAIM, "status": "promoted", "confidence": "verified",
                "source_id": "claim_004", "trusted": True,
            }]})
        elif self.path.endswith("/capture"):
            type(self).captured.append(payload)
            self._send({"accepted": True, "candidate_id": "candidate_1",
                        "status": "pending"})
        else:
            self._send({})

    def log_message(self, *_args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def wikibrain():
    _WikiBrainHandler.captured = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WikiBrainHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _WikiBrainHandler
    server.shutdown()


# ------------------------------------------------------------------- fake agents
#: A Codex-shaped manager: no arguments, no database, no credentials. It reads the
#: environment `launch` prepared and drives AgentConnect through the CLI, exactly
#: as CODEX.md instructs. Every step it takes is a durable ledger write.
MANAGER_AGENT = '''
import json, os, subprocess, sys

AC = [sys.executable, "-c",
      "import sys;sys.path[:0]=%r;from agentconnect.cli.main import main;sys.exit(main())" % (SRC,)]

def ac(*args):
    result = subprocess.run(AC + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("agentconnect %s failed: %s" % (" ".join(args), result.stderr))
    return json.loads(result.stdout) if result.stdout.strip() else {}

task = os.environ["AGENTCONNECT_TASK_ID"]
me = os.environ["AGENTCONNECT_MANAGER_ID"]
report = {"cwd": os.getcwd(), "leaked": sorted(
    k for k in os.environ
    if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LINEAR_API_KEY",
             "TEMPORAL_ADDRESS", "WIKIBRAIN_ADMIN_TOKEN", "AWS_SECRET_ACCESS_KEY"))}

# 3. Required first action: pull the context pack.
pack = ac("tasks", "context-pack", task, "--profile", "manager_brief")
report["pack"] = pack

# 4. Record what we did, for the manager who replaces us.
report["attempt"] = ac("attempts", "add", task, "--actor", me,
                       "--summary", "Read the auth module and planned the split.")
report["decision"] = ac("decisions", "add", task, "--by", me, "--locked",
                        "--decision", "Consolidate expiry into auth/session.py.")

# 5. Delegate bounded work to a worker.
subtask = ac("subtasks", "submit", task, "--title", "scan expiry call sites",
             "--instructions", "list every expiry check")
report["subtask"] = subtask

# 8. Ask another manager to review what the worker produced.
report["review"] = ac("reviews", "request", task, "--by", me, "--to", "codex",
                      "--artifact", subtask["result_artifact_id"],
                      "--criteria", "Did it miss a call site?")

open(os.environ["AGENT_REPORT"], "w").write(json.dumps(report))
'''

#: A reviewer. Its session is scoped to a review; its token buys `complete_review`
#: and nothing that would let it decide, delegate, or complete the task.
REVIEWER_AGENT = '''
import json, os, subprocess, sys

AC = [sys.executable, "-c",
      "import sys;sys.path[:0]=%r;from agentconnect.cli.main import main;sys.exit(main())" % (SRC,)]

def ac(*args):
    result = subprocess.run(AC + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("agentconnect %s failed: %s" % (" ".join(args), result.stderr))
    return json.loads(result.stdout) if result.stdout.strip() else {}

task = os.environ["AGENTCONNECT_TASK_ID"]
review = os.environ["AGENTCONNECT_REVIEW_ID"]
me = os.environ["AGENTCONNECT_MANAGER_ID"]
report = {"mode": os.environ["AGENTCONNECT_MODE"]}

# 9. A reviewer pack: trusted claims and the handoff, never Cognee breadth.
report["pack"] = ac("tasks", "context-pack", task, "--profile", "reviewer_brief")
report["attempt"] = ac("attempts", "add", task, "--actor", me,
                       "--summary", "Read the worker artifact against the criteria.")

# 10. Answer with a durable artifact, not a chat message.
report["review"] = ac("reviews", "complete", review, "--by", me,
                      "--summary", "No call sites missed.",
                      "--content", "Checked 4 sites; all covered.")

open(os.environ["AGENT_REPORT"], "w").write(json.dumps(report))
'''


def _write_agent(tmp_path, name, body):
    """The fake agent gets no PYTHONPATH, so inline the src paths it imports from."""
    path = tmp_path / name
    path.write_text(f"SRC = {SRC!r}\n" + body)
    return path


def _cli(argv, cwd, env):
    code = (f"import sys;sys.path[:0]={SRC!r};"
            "from agentconnect.cli.main import main;sys.exit(main())")
    return subprocess.run([sys.executable, "-c", code, *argv], cwd=str(cwd), env=env,
                          capture_output=True, text=True)


@pytest.fixture()
def operator_env(tmp_path, wikibrain):
    """What the box actually has lying around: a ledger, and a lot of credentials."""
    base_url, _ = wikibrain
    memory_yaml = tmp_path / "memory.yaml"
    memory_yaml.write_text(
        "memory:\n"
        "  enabled: true\n"
        "  trusted_authority: wikibrain\n"
        "  backends:\n"
        "    wikibrain:\n"
        "      enabled: true\n"
        f"      base_url: {base_url}\n"
        "  default_scopes:\n"
        "    project: fascia\n"
        "    repo: mcp-agentconnect\n"
    )
    env = dict(os.environ)
    env.update({
        "AGENTCONNECT_DB_PATH": str(tmp_path / "ledger.db"),
        "AGENTCONNECT_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "AGENTCONNECT_WORKSPACE_DIR": str(tmp_path / "workspaces"),
        "AGENTCONNECT_MEMORY_CONFIG": str(memory_yaml),
        "ANTHROPIC_API_KEY": "sk-ant-real", "OPENAI_API_KEY": "sk-oai-real",
        "LINEAR_API_KEY": "lin_real", "TEMPORAL_ADDRESS": "localhost:7233",
        "WIKIBRAIN_ADMIN_TOKEN": "wb-admin", "AWS_SECRET_ACCESS_KEY": "aws-real",
    })
    return env


@pytest.fixture()
def operator(operator_env):
    """The parent process: the human's view of the same ledger the agents write to."""
    svc = AgentConnectService.create(
        db_path=operator_env["AGENTCONNECT_DB_PATH"],
        artifact_dir=operator_env["AGENTCONNECT_ARTIFACT_DIR"],
        workspace_dir=operator_env["AGENTCONNECT_WORKSPACE_DIR"],
        workers=[EchoWorker()],
    )
    yield svc
    svc.storage.close()


def _run_agent(tmp_path, env, agent_path, report_path, *shell_args):
    # `AGENT_REPORT` is how the fake agent tells us what it saw. It survives
    # sanitization only through the sanctioned opt-in, which is the point: nothing
    # reaches an agent's environment by accident.
    env = {**env, "AGENT_REPORT": str(report_path),
           "AGENTCONNECT_SHELL_ALLOW_ENV": "AGENT_REPORT"}
    result = _cli(["shell", *shell_args, "--", sys.executable, str(agent_path)],
                  tmp_path, env)
    assert result.returncode == 0, f"agent failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(report_path.read_text())


# ============================================================== the whole loop
def test_the_proprietary_agent_loop_forces_durable_work_through_agentconnect(
    tmp_path, operator, operator_env, wikibrain
):
    _, wikibrain_handler = wikibrain
    task = operator.create_task(CreateTaskRequest(
        title="Refactor auth expiry", goal="dedupe refresh-token expiry",
        constraints=[CONSTRAINT], created_by="matthew",
        metadata={"repo_id": "mcp-agentconnect"}))

    # 1. launch --claim ------------------------------------------------------
    launched = _cli(["launch", "claude", "--task", task.id, "--claim"], tmp_path,
                    operator_env)
    assert launched.returncode == 0, launched.stderr
    assert "Prepared AgentConnect session." in launched.stdout
    assert operator.get_task(task.id).task.current_manager == "claude"

    workspace = operator.workspace_for(task_id=task.id)
    session = operator.active_session_for(task_id=task.id)
    assert (Path(workspace.path) / "CLAUDE.md").exists()

    # 2-5, 8. shell -- <agent> ------------------------------------------------
    agent = _write_agent(tmp_path, "manager_agent.py", MANAGER_AGENT)
    report = _run_agent(tmp_path, operator_env, agent, tmp_path / "manager.json",
                        "--task", task.id)

    # 7 (a): the agent ran where AgentConnect put it, with no credentials.
    assert report["leaked"] == []
    assert report["cwd"] == str(Path(workspace.repo_path).resolve())

    # 3: the pack is ledger truth plus clearly-labeled external context. The
    #    trusted claim came over real HTTP from a real WikiBrain.
    pack = report["pack"]
    assert pack["memory_is_external_context"] is True
    assert pack["backends_queried"] == ["wikibrain"]
    assert pack["scopes_queried"] == [
        "global", "project:fascia", "repo:mcp-agentconnect", f"task:{task.id}",
        "manager:claude"]
    claim = next(i for i in pack["memory"]["items"] if i["source_id"] == "claim_004")
    assert claim["trusted"] is True and claim["text"] == TRUSTED_CLAIM
    assert CONSTRAINT in [i["text"] for i in pack["memory"]["items"]]

    # 4-5: the work is in the ledger, not in the agent's context window. Both the
    # manager and the worker it delegated to left a record of what they did.
    detail = operator.get_task(task.id)
    by_actor = {a.actor_id: a.summary for a in detail.attempts}
    assert by_actor["claude"] == "Read the auth module and planned the split."
    assert "echo_worker" in by_actor
    assert detail.decisions[0].locked is True
    assert detail.decisions[0].made_by == "claude"
    subtask_id = report["subtask"]["id"]

    # 6: the worker_brief was injected before the worker ran.
    subtask = operator.get_subtask(subtask_id).subtask
    injected = subtask.metadata["context_pack"]
    assert injected["profile"] == "worker_brief"
    assert injected["memory_is_external_context"] is True
    assert TRUSTED_CLAIM in [i["text"] for i in injected["items"]]
    # A bounded worker gets no manager scope, and no manager debate.
    assert not any(s.startswith("manager:") for s in injected["scopes_queried"])

    # 7 (b): the worker produced a registered artifact.
    assert subtask.status.value == "succeeded"
    body = operator.read_artifact_chunk(subtask.result_artifact_id, 0, 8000).content
    assert "list every expiry check" in body

    # The manager's shell session was recorded start to end, and its token is dead.
    assert operator.get_session(session.id).status is SessionStatus.ended
    with pytest.raises(Unauthenticated, match="revoked"):
        operator.authorize(_token_of(workspace), "record_attempt")

    # 8: a review ticket, not a chat message.
    review_id = report["review"]["id"]
    assert operator.get_task(task.id).task.status is TaskStatus.needs_review

    # 9-10. the reviewer, in its own session ---------------------------------
    reviewer_launch = _cli(["launch", "codex", "--review", review_id, "--claim"],
                           tmp_path, operator_env)
    assert reviewer_launch.returncode == 0, reviewer_launch.stderr
    assert "Mode: reviewer" in reviewer_launch.stdout

    reviewer = _write_agent(tmp_path, "reviewer_agent.py", REVIEWER_AGENT)
    review_report = _run_agent(tmp_path, operator_env, reviewer, tmp_path / "codex.json",
                               "--review", review_id)

    assert review_report["mode"] == "reviewer"
    reviewer_pack = review_report["pack"]
    assert "cognee" not in reviewer_pack["backends_queried"]
    assert reviewer_pack["handoff"] is not None  # a reviewer judges a manager's work
    assert operator.get_review(review_id).status.value == "completed"
    assert operator.get_review(review_id).result_artifact_id

    # 11. the audit ----------------------------------------------------------
    audit = operator.audit_task(task.id)
    assert audit.passed, audit.problems
    assert "PASS" in audit.render()

    # 12. completion updates AgentConnect, and *then* Linear ------------------
    order: list[str] = []

    def linear_hook(task_id: str) -> None:
        assert operator.get_task(task_id).task.status is TaskStatus.succeeded
        order.append("linear")

    linear_hook.__name__ = "linear_post_completion"
    operator.bind_completion_hook(linear_hook)

    result = operator.complete_task(task.id, completed_by="matthew")
    assert result["status"] == "succeeded" and result["audit"]["status"] == "PASS"
    assert order == ["linear"]
    assert operator.get_task(task.id).task.status is TaskStatus.succeeded


def _token_of(workspace) -> str:
    from agentconnect.core.sessions import parse_env_file

    env = parse_env_file((Path(workspace.path) / ".env.agentconnect").read_text())
    return env["AGENTCONNECT_SESSION_TOKEN"]


# ============================================== the loop refuses to be short-cut
def test_an_agent_that_records_nothing_cannot_complete_its_task(
    tmp_path, operator, operator_env
):
    """The whole point. An agent may think in its own harness; if it never wrote to
    the ledger, the task does not complete and the audit says exactly why."""
    task = operator.create_task(CreateTaskRequest(title="Silent work", goal="g"))
    assert _cli(["launch", "claude", "--task", task.id, "--claim"], tmp_path,
                operator_env).returncode == 0

    idle = tmp_path / "idle_agent.py"
    idle.write_text("print('I thought about it a lot.')\n")
    ran = _cli(["shell", "--task", task.id, "--", sys.executable, str(idle)],
               tmp_path, operator_env)
    assert ran.returncode == 0  # the agent believes it succeeded

    audit = operator.audit_task(task.id)
    assert not audit.passed
    assert "No record_attempt was made during this session." in audit.problems

    with pytest.raises(PolicyViolation, match="audit failed"):
        operator.complete_task(task.id, "matthew")
    assert operator.get_task(task.id).task.status is not TaskStatus.succeeded


def test_a_managed_agent_never_holds_a_credential_that_completes_its_own_task(
    tmp_path, operator, operator_env
):
    task = operator.create_task(CreateTaskRequest(title="t", goal="g"))
    assert _cli(["launch", "claude", "--task", task.id, "--claim"], tmp_path,
                operator_env).returncode == 0
    token = _token_of(operator.workspace_for(task_id=task.id))

    assert operator.authorize(token, "record_attempt")["mode"] == "manager"
    for forbidden in ("complete_task", "promote_memory_candidate", "grant_approval",
                      "temporal_signal", "secrets_read"):
        with pytest.raises(PolicyViolation):
            operator.authorize(token, forbidden)
