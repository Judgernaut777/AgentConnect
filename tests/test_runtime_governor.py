"""Final-invocation-boundary tool governance in the in-process act/tool loop
(ADR 0009): every side-effecting tool call the LangGraph runtime makes is
authorized AND redeemed against its EXACT final arguments immediately before it
executes — not the model's declared tool set (that pre-spawn check, site #2 in
`core/service.py`, stays a cheap early filter), but the literal args about to run.

Self-contained by repo convention (see `test_browser.py`): the scripted model
source and a local `FakeGovernor` are copied here, not imported across test
modules. `FakeGovernor` mirrors ToolConnect's real one-use-grant semantics
(bind principal + exact args at issue, enforce both at redeem) closely enough to
prove the runtime-side wiring without a live server.
"""

from __future__ import annotations

import json

import pytest

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.runtime import LangGraphAgentRuntime, RuntimeConfig
from agentconnect.core.toolconnect_client import RedeemResult, ToolDecision, ToolGrant


class ScriptedModelSource:
    """Replays a fixed sequence of model replies; repeats the last one."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[GenerateRequest] = []

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.requests.append(req)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


def _finish(summary: str = "done") -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9})


class FakeGovernor:
    """Deterministic in-process `ToolGovernor` implementing the contract-1.1
    grant/redeem surface. `deny`/`raise_on` gate `authorize`; `redeem_deny` makes
    `redeem` fail (with the given reason) for named tools even though authorize
    allowed — the case a governor with a live but rejecting grant path produces."""

    mode = "required"

    def __init__(self, *, deny=(), raise_on=(), redeem_deny=None, redeem_raise_on=()):
        self.deny = set(deny)
        self.raise_on = set(raise_on)
        self.redeem_deny = dict(redeem_deny or {})  # tool_name -> reason
        self.redeem_raise_on = set(redeem_raise_on)
        self.authorize_calls: list[tuple] = []
        self.redeem_calls: list[tuple] = []
        self.record_calls: list[tuple] = []
        self._grants: dict[str, dict] = {}

    def authorize(self, principal, source_id, name, context=None, *,
                  args=None, ttl_seconds=None):
        self.authorize_calls.append((source_id, name, dict(principal), args))
        if name in self.raise_on:
            raise RuntimeError("engine exploded")
        if name in self.deny:
            return ToolDecision(allowed=False, reason=f"policy forbids {name}",
                                decision_id=f"dec-{name}", default_deny=True)
        grant = None
        if args is not None:
            grant_id = f"g-{name}-{len(self._grants)}"
            self._grants[grant_id] = {
                "principal_id": principal.get("id"), "args": dict(args),
                "name": name, "source_id": source_id, "redeemed": False,
            }
            grant = ToolGrant(grant_id=grant_id, args_hash=f"hash-{name}",
                              expires_at="9999-01-01T00:00:00+00:00", ttl_seconds=60)
        return ToolDecision(allowed=True, reason="allowed", decision_id=f"dec-{name}",
                            grant=grant)

    def redeem(self, grant_id, principal, args):
        self.redeem_calls.append((grant_id, dict(principal), dict(args)))
        record = self._grants.get(grant_id)
        if record is None:
            return RedeemResult(False, reason="not_found", grant_id=grant_id)
        if record["name"] in self.redeem_raise_on:
            raise RuntimeError("redeem engine exploded")
        if record["name"] in self.redeem_deny:
            return RedeemResult(False, reason=self.redeem_deny[record["name"]],
                                grant_id=grant_id, decision_id=f"dec-{record['name']}")
        if record["redeemed"]:
            return RedeemResult(False, reason="already_redeemed", grant_id=grant_id,
                                decision_id=f"dec-{record['name']}")
        if principal.get("id") != record["principal_id"]:
            return RedeemResult(False, reason="principal_mismatch", grant_id=grant_id,
                                decision_id=f"dec-{record['name']}")
        if dict(args) != record["args"]:
            return RedeemResult(False, reason="args_mismatch", grant_id=grant_id,
                                decision_id=f"dec-{record['name']}")
        record["redeemed"] = True
        return RedeemResult(True, reason="ok", grant_id=grant_id,
                            decision_id=f"dec-{record['name']}",
                            source_id=record["source_id"], name=record["name"])

    def record(self, decision_id, outcome, detail=None, *, grant_id=None):
        self.record_calls.append((decision_id, outcome, dict(detail or {}), grant_id))
        return {"recorded": True}

    def health(self):
        return {"status": "ok"}


class RaisingRecordGovernor(FakeGovernor):
    """A governor whose `record` always raises — proves outcome-recording is
    best-effort and never crashes a successfully-executed run (M6 analogue)."""

    def record(self, decision_id, outcome, detail=None, *, grant_id=None):
        raise RuntimeError("audit sink is down")


class NoRedeemGovernor:
    """A governor that only implements `authorize` (no `redeem` at all) — stands
    in for a pre-1.1 / decision-only governor. The final boundary must refuse
    execution rather than silently downgrade to a bare-allow."""

    mode = "required"

    def __init__(self):
        self.authorize_calls: list[tuple] = []

    def authorize(self, principal, source_id, name, context=None, *,
                  args=None, ttl_seconds=None):
        self.authorize_calls.append((source_id, name, args))
        # Allows, but issues no grant at all (simulates a governor client that
        # never learned about grants) — decision.grant stays None.
        return ToolDecision(allowed=True, reason="allowed", decision_id=f"dec-{name}")

    def record(self, decision_id, outcome, detail=None, *, grant_id=None):
        return {"recorded": True}

    def health(self):
        return {"status": "ok"}


PRINCIPAL = {"id": "runtime-under-test", "kind": "agent", "privacy_tier": "local"}


def _runtime(replies, tmp_path, governor=None, **cfg):
    source = ScriptedModelSource(replies)
    config = RuntimeConfig(workspace_root=str(tmp_path), **cfg)
    rt = LangGraphAgentRuntime(
        source, config, tool_governor=governor, governed_principal=PRINCIPAL,
        governed_source_id="test-runtime",
    )
    return rt, source


# ------------------------------------------------------------- no governor bound
def test_no_governor_runs_unchanged(tmp_path):
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t1")
    assert result.status == "completed"
    assert (tmp_path / "a.txt").read_text() == "hi"


# --------------------------------------------------------- allow + redeem happy path
def test_write_file_allow_and_redeem_executes_with_exact_args(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hello"}), _finish()],
        tmp_path, governor=gov,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t2")
    assert result.status == "completed"
    assert (tmp_path / "a.txt").read_text() == "hello"
    assert len(gov.redeem_calls) == 1
    grant_id, principal, args = gov.redeem_calls[0]
    assert args == {"path": "a.txt", "content": "hello"}
    assert principal == PRINCIPAL


# ------------------------------------------------------------ authorize-deny
def test_authorize_deny_stops_execution_mid_loop(tmp_path):
    gov = FakeGovernor(deny={"write_file"})
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t3")
    assert not (tmp_path / "a.txt").exists()
    assert gov.redeem_calls == []  # deny short-circuits before any redeem attempt


# -------------------------------------------------------------- redeem-deny
def test_redeem_denied_means_no_execution(tmp_path):
    gov = FakeGovernor(redeem_deny={"write_file": "args_mismatch"})
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t4")
    assert not (tmp_path / "a.txt").exists()
    assert len(gov.redeem_calls) == 1  # redeem WAS attempted...
    # ...but the write never happened; the loop fed back an ERROR observation.
    obs_messages = [m["content"] for m in rt.model_source.requests[-1].messages
                    if m["role"] == "user"]
    assert any("args_mismatch" in m or "not redeemed" in m for m in obs_messages)


def test_redeem_raising_refuses_fail_closed(tmp_path):
    gov = FakeGovernor(redeem_raise_on={"write_file"})
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t5")
    assert not (tmp_path / "a.txt").exists()


def test_authorize_raising_refuses_fail_closed(tmp_path):
    gov = FakeGovernor(raise_on={"write_file"})
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t5b")
    assert not (tmp_path / "a.txt").exists()
    assert gov.redeem_calls == []


# --------------------------------------------------- legacy / no-grant governor
def test_no_grant_governor_refuses_ungoverned_execution(tmp_path):
    gov = NoRedeemGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t6")
    # Allowed, but no grant/redeem available -> must refuse, never execute ungoverned.
    assert not (tmp_path / "a.txt").exists()
    assert len(gov.authorize_calls) == 1


# --------------------------------------------------------- args are FINAL, not a template
def test_run_tests_binds_operator_command_not_model_args(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "run_tests", "command": "rm -rf /"}), _finish()],
        tmp_path, governor=gov, allow_tests=True, allow_shell=True,
        test_command="echo pinned-command",
    )
    rt.run(TaskSubmission(task="t"), task_id="t7")
    # The model's "command" arg (an attempted injection) must never reach the governor
    # or the shell — the bound/redeemed args are the OPERATOR's test_command.
    assert len(gov.authorize_calls) == 1
    _, name, _, args = gov.authorize_calls[0]
    assert name == "run_tests"
    assert args == {"command": "echo pinned-command"}
    grant_id, _, redeemed_args = gov.redeem_calls[0]
    assert redeemed_args == {"command": "echo pinned-command"}


def test_shell_final_args_match_what_executes(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "shell", "command": "echo hi"}), _finish()],
        tmp_path, governor=gov, allow_shell=True,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t8")
    assert "hi" in result.summary or result.status == "completed"
    args = gov.authorize_calls[0][3]
    assert args == {"command": "echo hi"}


# ------------------------------------------------------- local gate ordering
def test_allow_shell_false_denies_before_governor_consulted(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "shell", "command": "echo hi"}), _finish()],
        tmp_path, governor=gov, allow_shell=False,
    )
    rt.run(TaskSubmission(task="t"), task_id="t9")
    assert gov.authorize_calls == []  # the cheap local gate fired first


def test_delegate_never_touches_the_governor(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "delegate", "task": "sub-task"}), _finish()],
        tmp_path, governor=gov, allow_delegation=True,
    )
    rt.run(TaskSubmission(task="t"), task_id="t10")
    assert gov.authorize_calls == []


# --------------------------------------------------- args-is-not-None regression
def test_every_side_effecting_kind_authorizes_with_args_bound(tmp_path):
    gov = FakeGovernor()
    scripts = {
        "write_file": json.dumps({"action": "write_file", "path": "a.txt", "content": "x"}),
        "read_file": json.dumps({"action": "read_file", "path": "a.txt"}),
        "list_dir": json.dumps({"action": "list_dir", "path": "."}),
        "shell": json.dumps({"action": "shell", "command": "echo hi"}),
        "run_tests": json.dumps({"action": "run_tests"}),
    }
    (tmp_path / "a.txt").write_text("seed")
    for kind, reply in scripts.items():
        gov2 = FakeGovernor()
        rt, _ = _runtime([reply, _finish()], tmp_path, governor=gov2,
                         allow_shell=True, allow_tests=True)
        rt.run(TaskSubmission(task="t"), task_id=f"args-{kind}")
        assert len(gov2.authorize_calls) == 1, f"{kind} did not consult the governor"
        _, name, _, args = gov2.authorize_calls[0]
        assert name == kind
        assert args is not None, f"{kind} authorized with args=None (declared-set style, not final-args)"


# ------------------------------------------------------------ success-path recording
def test_record_raising_never_crashes_a_successful_run(tmp_path):
    gov = RaisingRecordGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish("ok")],
        tmp_path, governor=gov,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t11")
    assert result.status == "completed"
    assert (tmp_path / "a.txt").read_text() == "hi"


def test_record_called_with_executed_outcome_and_grant_id_on_success(tmp_path):
    gov = FakeGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t12")
    assert len(gov.record_calls) == 1
    decision_id, outcome, detail, grant_id = gov.record_calls[0]
    assert decision_id == "dec-write_file"
    assert outcome == "executed"
    assert detail["grant_id"].startswith("g-write_file")
    # The grant_id must ALSO travel as the dedicated keyword (=> a top-level body
    # field on the wire) — ToolConnect's close-via-outcome reads only that; a
    # grant_id buried in `detail` is opaque payload and closes nothing.
    assert grant_id == detail["grant_id"]


# ------------------------------------------------- TOCTOU: frozen args execute
def test_mutating_original_args_after_redeem_cannot_change_what_executes(tmp_path, monkeypatch):
    """M5 regression: the value that executes is the frozen mapping that was
    hashed/authorized/redeemed — mutating the model's original action-args dict
    between redeem and execute must NOT redirect or alter the side effect."""
    from agentconnect.runtime import graph as graph_mod

    captured: dict = {}
    real_parse = graph_mod.parse_action

    def spying_parse(text):
        action = real_parse(text)
        if action.kind == "write_file":
            captured["args"] = action.args  # the ORIGINAL mutable dict in state
        return action

    monkeypatch.setattr(graph_mod, "parse_action", spying_parse)

    class MutatingRedeemGovernor(FakeGovernor):
        def redeem(self, grant_id, principal, args):
            result = super().redeem(grant_id, principal, args)
            # Simulate anything touching the original args dict after redeem but
            # before execute (the TOCTOU window the frozen mapping closes).
            if "args" in captured:
                captured["args"]["path"] = "tampered.txt"
                captured["args"]["content"] = "TAMPERED"
            return result

    gov = MutatingRedeemGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hello"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t13")
    assert (tmp_path / "a.txt").read_text() == "hello"  # the redeemed args executed
    assert not (tmp_path / "tampered.txt").exists()     # the mutation went nowhere


# ------------------------------------------------- identity-echo defense-in-depth
def test_redeem_identity_echo_mismatch_refuses_execution(tmp_path):
    """A redemption that echoes a DIFFERENT tool/source identity than the one
    about to execute (wrong-grant bug, collision, compromised server) must be
    refused even though redeemed=True — mirrors ToolConnect's governed_invoke."""

    class WrongIdentityGovernor(FakeGovernor):
        def redeem(self, grant_id, principal, args):
            result = super().redeem(grant_id, principal, args)
            if result.redeemed:
                return RedeemResult(
                    True, reason="ok", grant_id=result.grant_id,
                    decision_id=result.decision_id,
                    source_id="some-other-source", name="some_other_tool",
                )
            return result

    gov = WrongIdentityGovernor()
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "a.txt", "content": "hi"}), _finish()],
        tmp_path, governor=gov,
    )
    rt.run(TaskSubmission(task="t"), task_id="t14")
    assert not (tmp_path / "a.txt").exists()
    assert len(gov.redeem_calls) == 1  # redeem happened; the echo check refused after
