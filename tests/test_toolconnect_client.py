"""AgentConnect-owned ToolConnect governor adapter (ToolConnect contract §6b).

The adapter must speak ToolConnect's HTTP decision API over httpx, authorize and record
over a real server, and — unlike every other AgentConnect adapter — fail *closed*: an
unreachable engine is a DENY, never an allow. It is never on the invocation data path.
These regressions exercise the real httpx path against a stub server plus the fail-closed
path against a dead port, and the from-config/service-binding wiring.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentconnect.core import bootstrap
from agentconnect.core.toolconnect_client import (
    RedeemResult,
    ToolConnectGovernor,
    ToolDecision,
    ToolGovernor,
    ToolGrant,
)


class _StubHandler(BaseHTTPRequestHandler):
    """A minimal ToolConnect decision point: /authorize, /decisions/{id}/outcome, /health."""

    recorded: list = []  # captured (decision_id, body) for record assertions

    def log_message(self, *args):  # silence the server's stderr chatter
        pass

    def _send(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"null")

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/authorize":
            body = self._read_json()
            name = body.get("name")
            args = body.get("args")
            # A server that requires a token: 401 unless the header is present.
            if name == "needs_auth" and not self.headers.get("Authorization"):
                self._send(401, {"error": "unauthorized"})
                return
            if name == "danger":
                self._send(200, {
                    "allowed": False, "reason": "policy forbids danger",
                    "decision_id": "dec-deny", "default_deny": False,
                    "determining_policies": ["no-danger"], "contract_version": "1.2",
                })
                return
            if name == "grantable" and args is not None:
                self._send(200, {
                    "allowed": True, "reason": "policy allows", "decision_id": "dec-grant",
                    "determining_policies": ["allow-grantable"], "contract_version": "1.1",
                    "grant": {"grant_id": "gr-1", "args_hash": "h", "ttl_seconds": 60,
                             "expires_at": "9999-01-01T00:00:00+00:00"},
                })
                return
            if name == "grantable_no_grant" and args is not None:
                # Simulates a pre-1.1 server that silently drops "args" and returns a
                # bare allow with no grant — the mixed-fleet case.
                self._send(200, {
                    "allowed": True, "reason": "policy allows", "decision_id": "dec-nogrant",
                    "contract_version": "1.0",
                })
                return
            self._send(200, {
                "allowed": True, "reason": "policy allows", "decision_id": "dec-ok",
                "determining_policies": ["allow-reads"], "contract_version": "1.0",
            })
        elif self.path.startswith("/decisions/") and self.path.endswith("/outcome"):
            decision_id = self.path.split("/")[2]
            body = self._read_json()
            type(self).recorded.append((decision_id, body))
            # The real server's wire shape: {"decision_id", "audit_seq"} — no
            # "recorded" key; that one is adapter-synthesized on outages only.
            self._send(200, {"decision_id": decision_id, "audit_seq": 1})
        elif self.path.startswith("/grants/") and self.path.endswith("/redeem"):
            grant_id = self.path.split("/")[2]
            body = self._read_json()
            args = body.get("args") or {}
            if grant_id == "gr-1":
                if args == {"path": "x"}:
                    self._send(200, {
                        "grant_id": grant_id, "decision_id": "dec-grant", "redeemed": True,
                        "reason": "ok", "source_id": "src", "name": "grantable",
                        "contract_version": "1.1",
                    })
                else:
                    self._send(200, {
                        "grant_id": grant_id, "decision_id": "dec-grant", "redeemed": False,
                        "reason": "args_mismatch", "source_id": "src", "name": "grantable",
                        "contract_version": "1.1",
                    })
                return
            if grant_id == "gr-broken-major":
                self._send(200, {
                    "grant_id": grant_id, "decision_id": "d", "redeemed": True,
                    "reason": "ok", "contract_version": "9.0",
                })
                return
            self._send(200, {
                "grant_id": grant_id, "decision_id": None, "redeemed": False,
                "reason": "not_found", "source_id": None, "name": None,
                "contract_version": "1.1",
            })
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture()
def stub_server():
    _StubHandler.recorded = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)  # port 0 -> fresh ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_adapter_is_a_tool_governor(stub_server):
    gov = ToolConnectGovernor(stub_server)
    assert isinstance(gov, ToolGovernor)  # structural: satisfies the Protocol seam
    assert not hasattr(gov, "invoke")  # never on the invocation data path


def test_authorize_allow_over_stub(stub_server):
    gov = ToolConnectGovernor(stub_server)
    decision = gov.authorize({"id": "agent-1", "privacy_tier": "local"}, "src", "read_file")
    assert isinstance(decision, ToolDecision)
    assert decision.allowed is True
    assert decision.unavailable is False
    assert decision.decision_id == "dec-ok"
    assert decision.determining_policies == ("allow-reads",)


def test_authorize_deny_is_a_normal_return(stub_server):
    gov = ToolConnectGovernor(stub_server)
    decision = gov.authorize({"id": "agent-1"}, "src", "danger")
    assert decision.allowed is False
    assert decision.unavailable is False  # a policy deny, not an outage
    assert decision.reason == "policy forbids danger"


def test_health_over_stub(stub_server):
    gov = ToolConnectGovernor(stub_server)
    assert gov.health() == {"status": "ok"}


def test_record_outcome_over_stub(stub_server):
    gov = ToolConnectGovernor(stub_server)
    result = gov.record("dec-ok", "succeeded", detail={"artifact": "a-1"})
    assert result["decision_id"] == "dec-ok"  # the server's shape, passed through
    assert "audit_seq" in result
    assert _StubHandler.recorded == [("dec-ok", {"outcome": "succeeded",
                                                 "detail": {"artifact": "a-1"}})]


def test_record_with_grant_id_sends_top_level_body_field(stub_server):
    # Close-via-outcome (contract 1.1): ToolConnect's POST /decisions/{id}/outcome
    # reads `grant_id` ONLY at the top level of the body — a grant_id tucked inside
    # `detail` is opaque payload and closes nothing. This is the wire-level
    # regression for the runtime's grant-closing outcome report.
    gov = ToolConnectGovernor(stub_server)
    result = gov.record("dec-g", "executed",
                        detail={"grant_id": "g-1", "tool": "write_file"}, grant_id="g-1")
    assert result["decision_id"] == "dec-g"
    ((decision_id, body),) = _StubHandler.recorded
    assert decision_id == "dec-g"
    assert body["grant_id"] == "g-1"  # top-level: the field the server acts on
    assert body["detail"] == {"grant_id": "g-1", "tool": "write_file"}


def test_record_without_grant_id_omits_the_field(stub_server):
    gov = ToolConnectGovernor(stub_server)
    gov.record("dec-plain", "succeeded", detail={"artifact": "a-1"})
    ((_, body),) = _StubHandler.recorded
    assert "grant_id" not in body  # byte-identical legacy body when not closing a grant


def test_token_sent_as_authorization_header(stub_server):
    # Server 401s the `needs_auth` tool unless Authorization is present; with a token
    # the adapter must supply it (and the 401 path must itself fail closed).
    denied = ToolConnectGovernor(stub_server).authorize({"id": "a"}, "src", "needs_auth")
    assert denied.allowed is False and denied.unavailable is True  # 401 -> fail closed

    with_token = ToolConnectGovernor(stub_server, token="Bearer t")
    allowed = with_token.authorize({"id": "a"}, "src", "needs_auth")
    assert allowed.allowed is True


def test_authorize_fail_closed_when_unreachable():
    # A port nothing is listening on: unreachable must be a DENY, never an allow.
    gov = ToolConnectGovernor("http://127.0.0.1:9", timeout=0.5)
    decision = gov.authorize({"id": "agent-1"}, "src", "read_file")
    assert decision.allowed is False
    assert decision.unavailable is True
    assert decision.default_deny is True


def test_health_unreachable_is_reported_not_raised():
    gov = ToolConnectGovernor("http://127.0.0.1:9", timeout=0.5)
    assert gov.health()["status"] == "unreachable"


def test_record_unreachable_is_best_effort():
    gov = ToolConnectGovernor("http://127.0.0.1:9", timeout=0.5)
    result = gov.record("dec-x", "succeeded")
    assert result["recorded"] is False  # never a crash on the audit path


# -- contract 1.1: argument-bound grants ------------------------------------


def test_authorize_with_args_puts_args_and_ttl_in_body_and_parses_grant(stub_server):
    gov = ToolConnectGovernor(stub_server)
    decision = gov.authorize(
        {"id": "a"}, "src", "grantable", args={"path": "x"}, ttl_seconds=30)
    assert decision.allowed is True
    assert isinstance(decision.grant, ToolGrant)
    assert decision.grant.grant_id == "gr-1"
    assert decision.grant.ttl_seconds == 60


def test_authorize_without_args_parses_no_grant(stub_server):
    gov = ToolConnectGovernor(stub_server)
    decision = gov.authorize({"id": "a"}, "src", "read_file")
    assert decision.allowed is True
    assert decision.grant is None


def test_authorize_allow_with_args_but_no_grant_is_mixed_fleet_fail_closed(stub_server):
    # A pre-1.1 server silently drops "args" and returns a bare allow: that must
    # NOT be treated as a real allow when args were sent — refuse fail-closed.
    gov = ToolConnectGovernor(stub_server)
    decision = gov.authorize(
        {"id": "a"}, "src", "grantable_no_grant", args={"path": "x"})
    assert decision.allowed is False
    assert decision.unavailable is True


def test_redeem_success_maps_all_fields(stub_server):
    gov = ToolConnectGovernor(stub_server)
    result = gov.redeem("gr-1", {"id": "a"}, {"path": "x"})
    assert isinstance(result, RedeemResult)
    assert result.redeemed is True
    assert result.reason == "ok"
    assert result.decision_id == "dec-grant"
    assert result.source_id == "src"
    assert result.name == "grantable"


def test_redeem_args_mismatch_is_a_normal_deny_not_an_exception(stub_server):
    gov = ToolConnectGovernor(stub_server)
    result = gov.redeem("gr-1", {"id": "a"}, {"path": "WRONG"})
    assert result.redeemed is False
    assert result.reason == "args_mismatch"
    assert result.unavailable is False


def test_redeem_not_found_is_a_normal_deny(stub_server):
    gov = ToolConnectGovernor(stub_server)
    result = gov.redeem("gr-does-not-exist", {"id": "a"}, {})
    assert result.redeemed is False
    assert result.reason == "not_found"


def test_redeem_incompatible_contract_major_fails_closed(stub_server):
    gov = ToolConnectGovernor(stub_server)
    result = gov.redeem("gr-broken-major", {"id": "a"}, {})
    assert result.redeemed is False
    assert result.unavailable is True


def test_redeem_fail_closed_when_unreachable():
    gov = ToolConnectGovernor("http://127.0.0.1:9", timeout=0.5)
    result = gov.redeem("gr-1", {"id": "a"}, {})
    assert result.redeemed is False
    assert result.unavailable is True


def test_redeem_missing_redeemed_key_is_unavailable_never_inferred_true():
    def transport(method, url, payload):
        return 200, {"grant_id": "gr-1", "decision_id": "d"}  # no "redeemed" key

    gov = ToolConnectGovernor("http://stub", transport=transport)
    result = gov.redeem("gr-1", {"id": "a"}, {})
    assert result.redeemed is False
    assert result.unavailable is True


def test_redeem_truthy_non_bool_redeemed_is_not_treated_as_true():
    def transport(method, url, payload):
        return 200, {"grant_id": "gr-1", "decision_id": "d", "redeemed": "true"}

    gov = ToolConnectGovernor("http://stub", transport=transport)
    result = gov.redeem("gr-1", {"id": "a"}, {})
    assert result.redeemed is False  # only a literal JSON `true` counts


def test_governor_satisfies_protocol_including_redeem(stub_server):
    gov = ToolConnectGovernor(stub_server)
    assert isinstance(gov, ToolGovernor)
    assert callable(gov.redeem)


def test_incompatible_contract_major_fails_closed():
    def transport(method, url, payload):
        return 200, {"allowed": True, "decision_id": "d", "contract_version": "9.0"}

    gov = ToolConnectGovernor("http://stub", transport=transport)
    decision = gov.authorize({"id": "a"}, "src", "read_file")
    assert decision.allowed is False  # a shape we cannot read is not an allow
    assert decision.unavailable is True


# -- bootstrap wiring -------------------------------------------------------------


def test_governor_from_env_absent_config_is_none(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCONNECT_TOOLCONNECT_URL", raising=False)
    monkeypatch.setenv(bootstrap.TOOLCONNECT_CONFIG_PATH, str(tmp_path / "absent.yaml"))
    assert bootstrap.toolconnect_governor_from_env() is None


def test_governor_from_env_builds_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv(bootstrap.TOOLCONNECT_CONFIG_PATH, str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_URL", "http://127.0.0.1:8095/")
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_TOKEN", "Bearer tc")
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_MODE", "advisory")

    gov = bootstrap.toolconnect_governor_from_env()
    assert isinstance(gov, ToolConnectGovernor)
    assert gov.base_url == "http://127.0.0.1:8095"
    assert gov.token == "Bearer tc"
    assert gov.mode == "advisory"


def test_governor_from_env_malformed_degrades_to_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTCONNECT_TOOLCONNECT_URL", raising=False)
    bad = tmp_path / "toolconnect.yaml"
    bad.write_text("toolconnect: [bad: yaml, : :\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.TOOLCONNECT_CONFIG_PATH, str(bad))
    assert bootstrap.toolconnect_governor_from_env() is None


def test_service_from_env_binds_governor(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCONNECT_TOOLCONNECT_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("AGENTCONNECT_DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("AGENTCONNECT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENTCONNECT_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("AGENTCONNECT_WORKERS", "echo")
    # No compute or memory configured for this test.
    monkeypatch.delenv("AGENTCONNECT_COMPUTE_URL", raising=False)
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(tmp_path / "absent-compute.yaml"))
    monkeypatch.setenv(bootstrap.MEMORY_CONFIG_PATH, str(tmp_path / "absent-mem.yaml"))

    service = bootstrap.service_from_env()
    assert isinstance(service.tool_governor, ToolConnectGovernor)
    assert service.tool_governor.base_url == "http://127.0.0.1:8095"
