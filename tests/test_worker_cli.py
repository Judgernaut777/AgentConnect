"""The `agentconnect-worker` CLI: arg parsing, TLS selection, and offline
construction of a configured PullWorker (dry-run echo model, no network)."""

import pytest

from agentconnect.common.schemas import GenerateRequest
from agentconnect.runtime.worker_cli import (
    _EchoModelSource,
    _parse_args,
    _tls_from_args,
    build_worker,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_build_worker_dry_run_is_offline_and_configured():
    args = _parse_args(
        [
            "--broker", "http://broker",
            "--capabilities", "coding, review ,summarization",
            "--dry-run", "--insecure-localhost",
            "--heartbeat-interval", "5", "--poll-interval", "0.5",
        ]
    )
    worker = build_worker(args)  # must not touch the network
    assert worker.capabilities == ["coding", "review", "summarization"]
    assert worker.heartbeat_interval == 5.0
    assert worker.poll_interval == 0.5
    # --dry-run wires the built-in echo model (no model server / model-manager).
    assert isinstance(worker.runtime.model_source, _EchoModelSource)


def test_echo_model_finishes_and_echoes_task():
    resp = _EchoModelSource().generate(
        GenerateRequest(
            request_id="r", task_id="t", model_id="m",
            messages=[{"role": "user", "content": "do the thing"}],
        )
    )
    assert "do the thing" in resp.output_text


def test_shell_defaults_on_browser_off_and_flags_flip():
    a = _parse_args(["--broker", "http://b", "--dry-run"])
    assert a.shell is True and a.browser is False
    b = _parse_args(["--broker", "http://b", "--dry-run", "--no-shell", "--browser"])
    assert b.shell is False and b.browser is True


def test_shell_flag_reaches_the_runtime_config():
    args = _parse_args(["--broker", "http://b", "--dry-run", "--no-shell"])
    worker = build_worker(args)
    assert worker.runtime.config.allow_shell is False


def test_tls_from_args_insecure_and_mutual():
    ins = _tls_from_args(_parse_args(["--broker", "http://b", "--dry-run", "--insecure-localhost"]))
    assert ins.mode == "insecure_localhost"

    mut = _tls_from_args(
        _parse_args(
            ["--broker", "https://b", "--dry-run",
             "--ca", "ca.pem", "--cert", "c.pem", "--key", "k.pem"]
        )
    )
    assert mut.mode == "mutual"
    assert mut.client_cert == "c.pem" and mut.ca_cert == "ca.pem"

    none_tls = _tls_from_args(_parse_args(["--broker", "http://b", "--dry-run"]))
    assert none_tls is None  # no material -> plain HTTP


def test_identity_flag_sets_the_dev_proxy_header():
    plain = build_worker(_parse_args(["--broker", "http://b", "--dry-run"]))
    assert plain._headers == {}  # real deployment: identity is the mTLS cert, no header

    tagged = build_worker(
        _parse_args(["--broker", "http://b", "--dry-run", "--identity", "trusted-worker"])
    )
    assert tagged._headers == {"X-Client-Cert-DN": "trusted-worker"}


def test_broker_is_required():
    with pytest.raises(SystemExit):
        _parse_args(["--dry-run"])
