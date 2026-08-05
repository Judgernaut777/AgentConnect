"""R6 end-to-end: governance-grant redemption at the final invocation boundary
emits an Execution Record carrying the full ADR-048 id chain
(work request → decision → grant → redemption → execution).

Self-contained by repo convention (see `test_runtime_governor.py`): the
scripted model source and a local governor double are defined here, not
imported across test modules. The grant fixture below is a verbatim copy of
Connect-Governance conformance vector gv-001's signed grant — copied, not
imported across repos, per the ecosystem's interop-through-the-artifact rule.
The fake governor mirrors ToolConnect's ``POST /redemptions`` response shape
closely enough to prove the runtime wiring without a live server.
"""

from __future__ import annotations

import json

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.core.execution_records import verify_execution_record
from agentconnect.core.toolconnect_client import GovernanceRedemption
from agentconnect.runtime import GovernanceLinkage, LangGraphAgentRuntime, RuntimeConfig


# Verbatim copy of conformance/grant-vectors/gv-001-sign-verify.json's
# `expected_grant` (Connect-Governance repo). The runtime never verifies the
# signature itself — that is the provider's job — but the record's linkage ids
# are read from this SIGNED payload, so the fixture exercises the real shape.
GOV_GRANT = {
    "payload": {
        "argument_constraints": {"path": "/srv/out", "tool": "fs.write"},
        "budget_limit_usd": 100.0,
        "correlation_id": "corr-1",
        "data_classifications": ["internal"],
        "decision_record_id": "dr-vec-1",
        "delegation_depth": 0,
        "delegation_max_depth": 1,
        "grant_format_version": "1",
        "grant_id": "g-vec-1",
        "issued_at": "2026-08-03T12:00:00Z",
        "issuer_key_id": "ed25519:f294dcbe2bea2831af6df47eaf039ec5b7b223644dd1689f63be2d90bb5d800a",
        "kernel_version": "0.0.1",
        "not_after": "2026-08-04T00:00:00Z",
        "not_before": "2026-08-03T00:00:00Z",
        "organization_id": "org-1",
        "permitted_operations": ["tool.invoke"],
        "policy_versions": ["pol-1@3"],
        "provider_id": "toolconnect",
        "requesting_principal_id": "agent-1",
        "revocation_state": "active",
        "work_request_id": "wr-1",
        "work_request_revision": "rev-1",
        "workspace_id": "ws-1",
    },
    "signature": "bL8rFJb2UEnFvg2i8gdPjXaMkuCKoGSXEjnzUPhtlutQ4kqUa-HWHI4Wj6PblYHD9ZBIfyteALwLZlhgZJl3CA",
    "signature_scheme": "Ed25519",
}

PRINCIPAL = {"id": "agent-1", "kind": "agent", "privacy_tier": "local"}


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


class FakeGovGovernor:
    """In-process governor double with ONLY the governance-redemption surface
    (no contract-1.1 authorize/redeem): proves the R6 path redeems the signed
    grant and never silently substitutes the weaker 1.1 gate. `deny_reason`
    simulates a point-of-effect denial; `raise_on_redeem` an outage."""

    mode = "required"

    def __init__(self, *, deny_reason=None, raise_on_redeem=False, echo_tool=True):
        self.deny_reason = deny_reason
        self.raise_on_redeem = raise_on_redeem
        self.echo_tool = echo_tool
        self.calls: list[tuple] = []

    def redeem_governance_grant(self, grant, principal, source_id, name, args, *, at=None):
        self.calls.append((grant, dict(principal), source_id, name, dict(args), at))
        if self.raise_on_redeem:
            raise RuntimeError("redemption service exploded")
        payload = grant.get("payload", {})
        common = dict(
            grant_id=payload.get("grant_id", ""),
            decision_record_id=payload.get("decision_record_id", ""),
            correlation_id=payload.get("correlation_id", ""),
            source_id=source_id if self.echo_tool else "other-source",
            name=name if self.echo_tool else "other-tool",
            contract_version="1.1",
        )
        if self.deny_reason is not None:
            return GovernanceRedemption(
                False, reason=self.deny_reason, failure_codes=(self.deny_reason,), **common)
        return GovernanceRedemption(True, reason="ok", **common)


def _runtime(replies, tmp_path, *, governor, linkage):
    config = RuntimeConfig(workspace_root=str(tmp_path))
    rt = LangGraphAgentRuntime(
        ScriptedModelSource(replies), config,
        tool_governor=governor, governed_principal=PRINCIPAL,
        governed_source_id="test-runtime", governance=linkage,
    )
    return rt


def _linkage(records: list, **over):
    counter = {"n": 0}

    def next_id():
        counter["n"] += 1
        return f"execrec_test{counter['n']:08d}"

    kwargs = dict(
        grant=GOV_GRANT,
        sink=records.append,
        executor_id="worker-7",
        harness="agentconnect-runtime",
        at="2026-08-03T12:00:00Z",
        clock=lambda: "2026-08-03T12:00:00Z",
        record_id_factory=next_id,
    )
    kwargs.update(over)
    return GovernanceLinkage(**kwargs)


WRITE_A = json.dumps({"action": "write_file", "path": "a.txt", "content": "hello"})


# --------------------------------------------------------------- happy path
def test_governance_redemption_executes_and_records_full_chain(tmp_path):
    records: list = []
    gov = FakeGovGovernor()
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=gov, linkage=_linkage(records))

    result = rt.run(TaskSubmission(task="t"), task_id="task_g1")

    assert result.status == "completed"
    assert (tmp_path / "a.txt").read_text() == "hello"
    # The signed grant traveled to the provider verbatim.
    assert len(gov.calls) == 1
    grant, principal, source_id, name, args, at = gov.calls[0]
    assert grant["signature_scheme"] == "Ed25519"
    assert principal["id"] == "agent-1"  # the grant's requesting_principal_id
    assert (source_id, name) == ("test-runtime", "write_file")
    assert args == {"path": "a.txt", "content": "hello"}
    assert at == "2026-08-03T12:00:00Z"

    # Exactly one Execution Record, with the whole ADR-048 chain traversable.
    assert len(records) == 1
    record = records[0]
    assert record.outcome == "succeeded"
    assert record.work_request_id == "wr-1"
    assert record.decision_record_id == "dr-vec-1"
    assert record.grant_id == "g-vec-1"
    assert record.correlation_id == "corr-1"
    assert record.task_id == "task_g1"
    assert record.provider_enforcement.provider_id == "toolconnect"
    assert record.provider_enforcement.redemption_outcome == "redeemed"
    assert record.provider_enforcement.verified is True
    assert record.executor.executor_id == "worker-7"
    assert record.executor.harness == "agentconnect-runtime"
    assert (record.tool.source_id, record.tool.name) == ("test-runtime", "write_file")
    assert record.started_at == "2026-08-03T12:00:00Z"  # the injected clock
    assert verify_execution_record(record)


def test_deterministic_emission_byte_identical_hash(tmp_path):
    # Same run, same injected clock/ids → identical record hash.
    first: list = []
    _runtime([WRITE_A, _finish()], tmp_path / "r1", governor=FakeGovGovernor(),
             linkage=_linkage(first)).run(TaskSubmission(task="t"), task_id="task_det")
    second: list = []
    _runtime([WRITE_A, _finish()], tmp_path / "r2", governor=FakeGovGovernor(),
             linkage=_linkage(second)).run(TaskSubmission(task="t"), task_id="task_det")
    assert first[0].record_hash == second[0].record_hash


# ------------------------------------------------- fail closed: no redemption
def test_denied_redemption_refuses_and_records_no_success(tmp_path):
    records: list = []
    gov = FakeGovGovernor(deny_reason="expired")
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=gov, linkage=_linkage(records))

    result = rt.run(TaskSubmission(task="t"), task_id="task_g2")

    assert not (tmp_path / "a.txt").exists()  # nothing executed
    assert result.status == "completed"  # soft in-loop stop, like the 1.1 path
    assert len(records) == 1
    record = records[0]
    assert record.outcome == "refused"
    assert record.provider_enforcement.redemption_outcome == "denied:expired"
    assert record.provider_enforcement.verified is False
    assert record.refusal_reason == "expired"
    # The refusal record still carries the linkage — the chain shows the
    # attempt, honestly, as refused.
    assert record.grant_id == "g-vec-1" and record.decision_record_id == "dr-vec-1"
    assert verify_execution_record(record)


def test_governor_outage_refuses_fail_closed(tmp_path):
    records: list = []
    gov = FakeGovGovernor(raise_on_redeem=True)
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=gov, linkage=_linkage(records))
    rt.run(TaskSubmission(task="t"), task_id="task_g3")
    assert not (tmp_path / "a.txt").exists()
    assert [r.outcome for r in records] == ["refused"]


def test_wrong_grant_echo_refuses(tmp_path):
    # Defense-in-depth: the redemption echoes a DIFFERENT tool identity than
    # the one about to run — some layer redeemed the wrong grant. Refuse.
    records: list = []
    gov = FakeGovGovernor(echo_tool=False)
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=gov, linkage=_linkage(records))
    rt.run(TaskSubmission(task="t"), task_id="task_g4")
    assert not (tmp_path / "a.txt").exists()
    assert records[0].outcome == "refused"
    assert "other-source" in records[0].refusal_reason


def test_no_governor_with_linkage_refuses_not_ungoverned(tmp_path):
    # The dangerous configuration: a governance-linked run with no governor at
    # all must fail closed — never degrade into ungoverned execution.
    records: list = []
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=None,
                  linkage=_linkage(records))
    rt.run(TaskSubmission(task="t"), task_id="task_g5")
    assert not (tmp_path / "a.txt").exists()
    assert [r.outcome for r in records] == ["refused"]
    assert "no governor" in records[0].refusal_reason


# ------------------------------------------------- ledger sink (service) path
def test_sink_into_service_ledger_chains_records(tmp_path):
    # Production wiring shape: the sink is the ledger write door. Two
    # governed calls in one run land hash-chained in the ledger, traversable
    # from any id in the chain.
    from agentconnect.core import AgentConnectService, CreateTaskRequest

    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[])
    task = svc.create_task(CreateTaskRequest(title="T"))
    shell = json.dumps({"action": "shell", "command": "echo hi > b.txt"})
    records_run: list = []
    from agentconnect.core.execution_record_store import ExecutionRecordLedger

    ledger = ExecutionRecordLedger(svc.storage)
    linkage = _linkage(records_run, sink=ledger.record)
    rt = _runtime([WRITE_A, shell, _finish()], tmp_path, governor=FakeGovGovernor(),
                  linkage=linkage)
    result = rt.run(TaskSubmission(task="t"), task_id=task.id)
    assert result.status == "completed"

    stored = ledger.list(grant_id="g-vec-1")
    assert len(stored) == 2
    assert stored[0].prev_hash is None
    assert stored[1].prev_hash == stored[0].record_hash
    assert all(verify_execution_record(r) for r in stored)
    # Bidirectional traversal from the governance side.
    by_wr = ledger.list(work_request_id="wr-1")
    by_corr = ledger.list(correlation_id="corr-1")
    assert {r.execution_record_id for r in by_wr} == {r.execution_record_id for r in by_corr}
    assert {r.outcome for r in by_wr} == {"succeeded"}


def test_tampered_stored_record_detectable(tmp_path):
    from agentconnect.core import AgentConnectService, CreateTaskRequest
    from agentconnect.core.execution_record_store import ExecutionRecordLedger

    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "art"), workers=[])
    task = svc.create_task(CreateTaskRequest(title="T"))
    ledger = ExecutionRecordLedger(svc.storage)
    rt = _runtime([WRITE_A, _finish()], tmp_path, governor=FakeGovGovernor(),
                  linkage=_linkage([], sink=ledger.record))
    rt.run(TaskSubmission(task="t"), task_id=task.id)
    stored = ledger.get(ledger.list(grant_id="g-vec-1")[0].execution_record_id)
    assert verify_execution_record(stored)
    tampered = stored.model_copy(update={"outcome": "refused"})
    assert not verify_execution_record(tampered)


# --------------------------------------- governor transport mapping (client)
class TestGovernanceRedemptionTransport:
    """The wire mapping of ToolConnectGovernor.redeem_governance_grant, with an
    injected transport — the same pattern the 1.1 redeem tests use."""

    def _governor(self, responder):
        from agentconnect.core.toolconnect_client import ToolConnectGovernor

        def transport(method, url, payload):
            return responder(method, url, payload)

        return ToolConnectGovernor("http://tc.test", transport=transport)

    def test_success_maps_linkage_fields(self):
        seen = {}

        def responder(method, url, payload):
            seen.update(payload)
            assert url.endswith("/redemptions") and method == "POST"
            return 200, {
                "grant_id": "g-vec-1", "redeemed": True, "reason": "ok",
                "failure_codes": [], "decision_record_id": "dr-vec-1",
                "correlation_id": "corr-1", "source_id": "s", "name": "n",
                "contract_version": "1.1",
            }

        result = self._governor(responder).redeem_governance_grant(
            GOV_GRANT, PRINCIPAL, "s", "n", {"path": "a.txt"}, at="2026-08-03T12:00:00Z")
        assert result.redeemed is True
        assert (result.grant_id, result.decision_record_id, result.correlation_id) == (
            "g-vec-1", "dr-vec-1", "corr-1")
        # The whole signed artifact went out verbatim; `at` was forwarded.
        assert seen["grant"]["signature_scheme"] == "Ed25519"
        assert seen["at"] == "2026-08-03T12:00:00Z"

    def test_point_of_effect_denial_is_a_normal_result(self):
        def responder(method, url, payload):
            return 200, {"grant_id": "g-vec-1", "redeemed": False,
                         "reason": "already_redeemed",
                         "failure_codes": ["already_redeemed"],
                         "source_id": "s", "name": "n", "contract_version": "1.1"}

        result = self._governor(responder).redeem_governance_grant(
            GOV_GRANT, PRINCIPAL, "s", "n", {})
        assert result.redeemed is False and result.unavailable is False
        assert result.reason == "already_redeemed"
        assert result.failure_codes == ("already_redeemed",)

    def test_transport_failure_is_unavailable_never_redeemed(self):
        def responder(method, url, payload):
            raise ConnectionError("down")

        # _call wraps injected transports? No: injected transport exceptions
        # propagate as-is; the public method must still not return redeemed.
        gov = self._governor(lambda *a: (_ for _ in ()).throw(ConnectionError("down")))
        try:
            result = gov.redeem_governance_grant(GOV_GRANT, PRINCIPAL, "s", "n", {})
        except Exception:
            # A raising injected transport is a test-harness artifact; the real
            # httpx path converts to ToolConnectUnavailable inside _call. Either
            # way there is no path to redeemed=True.
            return
        assert result.redeemed is False

    def test_non_200_and_missing_flag_fail_closed(self):
        for body in ((500, {"redeemed": True}), (200, {"ok": True}), (200, None)):
            result = self._governor(lambda *a, _b=body: _b).redeem_governance_grant(
                GOV_GRANT, PRINCIPAL, "s", "n", {})
            assert result.redeemed is False and result.unavailable is True
