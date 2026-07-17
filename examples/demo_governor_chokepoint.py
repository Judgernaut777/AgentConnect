"""End-to-end: the ToolConnect governor is really consulted, and fails closed.

The HIGH fix (ADR 0008) turned a dead seam into a real chokepoint. This demo proves
it against a **live** ToolConnect decision point — a real `toolconnect serve` on a
scratch port with a scratch DB and a Cedar policy that ALLOWS one tool and FORBIDS
another. No mocks on the AgentConnect side: `ToolConnectGovernor` talks to the server
over httpx exactly as a deployment would.

Run it (uses ToolConnect's own venv to run the server, this repo's venv for the rest):

    .venv/bin/python examples/demo_governor_chokepoint.py

What it shows, in order:

  1. ALLOWED: a worker declaring only `read_notes` (a READ tool the policy permits) is
     authorized — its subtask runs, and a `tool.authorized` event carries a REAL
     ToolConnect decision_id.
  2. FORBIDDEN: a worker declaring `delete_notes` (a WRITE tool no policy permits) is
     DENIED — its subtask is blocked before the worker spawns (no run, no artifact),
     with a `subtask.denied` event.
  3. FAIL CLOSED: ToolConnect is killed; the next authorization cannot reach the
     engine, so it denies (unavailable) rather than proceeding unconstrained.

Everything is offline and local. The server binds 127.0.0.1 on a fresh port; the DB
and policy live in a scratch dir that is removed on exit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "packages" / "agentconnect-core" / "src"))
sys.path.insert(0, str(_ROOT / "packages" / "agentconnect-mcp" / "src"))

from agentconnect.core import (  # noqa: E402
    AgentConnectService,
    ArtifactType,
    CreateTaskRequest,
    PrivacyTier,
    SubtaskRequest,
    WorkerAdapter,
    WorkerCapabilities,
    WorkerLocation,
    WorkerResult,
)
from agentconnect.core.observability import (  # noqa: E402
    CompositeObservabilityProvider,
    ObservabilityEmitter,
    StructuredLogObservabilityProvider,
)
from agentconnect.core.toolconnect_client import ToolConnectGovernor  # noqa: E402
from agentconnect.core.workers import WorkerArtifactRef  # noqa: E402

# The sibling ToolConnect checkout supplies the *server* (and its cedarpy dep) — the
# demo stands up a real one. AgentConnect never imports it; it only speaks HTTP.
TOOLCONNECT_ROOT = Path(os.environ.get("TOOLCONNECT_ROOT", "/home/mini/ToolConnect"))
TOOLCONNECT_PY = TOOLCONNECT_ROOT / ".venv" / "bin" / "python"

HOST = "127.0.0.1"
PORT = 8123           # fresh port (avoids 8080/8091/8787/8090/8095)
BASE_URL = f"http://{HOST}:{PORT}"
SOURCE = "demo_harness"   # AgentConnect authorizes a worker's tools under its harness

POLICY = """\
// Local agents may invoke READ tools that do not read sensitive data.
@id("local-reads")
permit(principal, action == Action::"invoke", resource)
when { principal.privacy_tier == "local" && resource.effect == "read" &&
       !resource.reads_sensitive };
// Everything else is Cedar default-deny: a WRITE tool like delete_notes never matches
// a permit, so it is forbidden.
"""


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _wait_healthy(deadline: float) -> bool:
    while time.time() < deadline:
        try:
            status, _ = _http("GET", "/health")
            if status == 200:
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _seed_policy_catalog() -> None:
    """Register a source and assert two tools: read_notes (READ, allowed) and
    delete_notes (WRITE, forbidden). Assertion is the operator's security claim."""
    def _post(path, body, ok=(200,)):
        status, payload = _http("POST", path, body)
        if status not in ok:
            raise SystemExit(f"seed step {path} failed ({status}): {payload}")
        return payload

    _post("/sources", {
        "source_id": SOURCE, "tier": "known", "transport": "push",
        "declares": ["read_notes", "delete_notes"]})
    _post(f"/sources/{SOURCE}/tools", {"tools": [
        {"name": "read_notes", "claimed": {"read_only_hint": True}},
        {"name": "delete_notes", "claimed": {"destructive_hint": True}}]})
    _post("/assertions", {
        "source_id": SOURCE, "name": "read_notes",
        "descriptor": {"effect": "read", "asserted_by": "operator"}})
    _post("/assertions", {
        "source_id": SOURCE, "name": "delete_notes",
        "descriptor": {"effect": "write", "reversible": False,
                       "asserted_by": "operator"}})


class DemoWorker(WorkerAdapter):
    """A local worker whose declared tool set is what the governor authorizes. Its
    harness name is the ToolConnect source the tools live under."""

    def __init__(self, worker_id: str, tools: list[str]):
        self._id = worker_id
        self._tools = tools
        self.ran = False

    @property
    def worker_id(self) -> str:
        return self._id

    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            worker_id=self._id, harness=SOURCE, tools=self._tools,
            privacy_tiers=list(PrivacyTier),
            capability_tags=["echo", "inspect", "summarize", "generate"],
            location=WorkerLocation.local)

    def run(self, subtask, context) -> WorkerResult:
        self.ran = True
        art = context.create_artifact(
            type=ArtifactType.worker_output, content="notes read", summary="done")
        return WorkerResult(status="succeeded", summary="ran",
                            artifacts=[WorkerArtifactRef(artifact_id=art.id)])


def _tool_events(svc, task_id, kind):
    return [e for e in svc.observation_events(task_id=task_id)
            if e["event_type"] == kind]


def main() -> int:
    if not TOOLCONNECT_PY.exists():
        print(f"FAIL: need a ToolConnect venv at {TOOLCONNECT_PY}")
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="governor-demo-"))
    db = scratch / "toolconnect.db"
    policy = scratch / "policy.cedar"
    policy.write_text(POLICY)

    env = dict(os.environ, PYTHONPATH=str(TOOLCONNECT_ROOT / "src"))
    server = subprocess.Popen(
        [str(TOOLCONNECT_PY), "-m", "toolconnect.cli", "serve",
         "--db", str(db), "--policies", str(policy), "--host", HOST, "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    ok = True
    try:
        if not _wait_healthy(time.time() + 15):
            err = server.stderr.read().decode()[-800:] if server.stderr else ""
            print(f"FAIL: toolconnect serve never became healthy\n{err}")
            return 2
        print(f"[toolconnect] live at {BASE_URL} (scratch db {db.name})")
        _seed_policy_catalog()

        # Configure AgentConnect's governor at the live URL (fail-closed by contract).
        svc = AgentConnectService.create(
            db_path=":memory:", artifact_dir=str(scratch / "art"),
            workers=[DemoWorker("reader", ["read_notes"]),
                     DemoWorker("deleter", ["delete_notes"])])
        prov = StructuredLogObservabilityProvider(scratch / "events.jsonl")
        svc.bind_observability(
            ObservabilityEmitter(CompositeObservabilityProvider([prov]),
                                 redactor=svc.observation_redactor()))
        svc.bind_tool_governor(ToolConnectGovernor(BASE_URL))

        # --- (a) ALLOWED tool set proceeds, with a real decision_id ---------------
        task_a = svc.create_task(CreateTaskRequest(title="read the notes"))
        sub_a = svc.submit_subtask(task_a.id, SubtaskRequest(
            title="read", instructions="read notes", preferred_worker="reader",
            required_capabilities=["inspect"]))
        ta = _tool_events(svc, task_a.id, "tool.authorized")
        decision_id = ta[-1]["metadata"]["decision_id"] if ta else ""
        allowed_ok = (sub_a.status.value == "succeeded" and bool(decision_id)
                      and decision_id != "" and ta[-1]["metadata"]["allowed"] is True)
        print(f"\n(a) ALLOWED  read_notes -> subtask {sub_a.status.value}, "
              f"decision_id={decision_id!r}  {'PASS' if allowed_ok else 'FAIL'}")
        ok &= allowed_ok

        # --- (b) FORBIDDEN tool is denied and BLOCKS the subtask ------------------
        task_b = svc.create_task(CreateTaskRequest(title="delete the notes"))
        sub_b = svc.submit_subtask(task_b.id, SubtaskRequest(
            title="delete", instructions="delete notes", preferred_worker="deleter",
            required_capabilities=["inspect"]))
        denied = _tool_events(svc, task_b.id, "subtask.denied")
        runs_b = svc.get_subtask(sub_b.id).runs
        forbidden_ok = (sub_b.status.value == "failed" and runs_b == []
                        and sub_b.result_artifact_id is None and bool(denied)
                        and denied[-1]["metadata"]["denied_tool"] == f"{SOURCE}:delete_notes"
                        and denied[-1]["metadata"]["unavailable"] is False)
        print(f"(b) FORBIDDEN delete_notes -> subtask {sub_b.status.value}, "
              f"runs={len(runs_b)}, denied_tool="
              f"{denied[-1]['metadata']['denied_tool'] if denied else None!r}  "
              f"{'PASS' if forbidden_ok else 'FAIL'}")
        ok &= forbidden_ok

        # --- (c) FAIL CLOSED: kill ToolConnect, the next authorization denies -----
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        # Give the socket a moment to actually close.
        time.sleep(0.5)
        task_c = svc.create_task(CreateTaskRequest(title="read after outage"))
        sub_c = svc.submit_subtask(task_c.id, SubtaskRequest(
            title="read", instructions="read notes", preferred_worker="reader",
            required_capabilities=["inspect"]))
        denied_c = _tool_events(svc, task_c.id, "subtask.denied")
        failclosed_ok = (sub_c.status.value == "failed" and bool(denied_c)
                         and denied_c[-1]["metadata"]["unavailable"] is True)
        print(f"(c) FAIL CLOSED (engine down) read_notes -> subtask "
              f"{sub_c.status.value}, unavailable="
              f"{denied_c[-1]['metadata']['unavailable'] if denied_c else None}  "
              f"{'PASS' if failclosed_ok else 'FAIL'}")
        ok &= failclosed_ok
    finally:
        if server.poll() is None:
            server.kill()
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{'ALL SCENARIOS PASS' if ok else 'DEMO FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
