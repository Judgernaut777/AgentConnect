"""Mutual-TLS transport for the Router<->Model-Manager link (handoff Goal 1).

Proves the handshake enforces client certificates (the replacement for the old
shared bearer token) and that the per-identity allowlist middleware works. The
handshake test needs `openssl`; it skips cleanly when absent so the core suite
never hard-depends on it.
"""

import shutil
import socket
import subprocess
import threading
import time

import pytest

from agentconnect.common.config import TlsClientConfig
from agentconnect.model_manager.app import create_app
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.local_client import HttpLocalClient

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("mtls")
    ca_crt, ca_key = str(d / "ca.crt"), str(d / "ca.key")
    srv_crt, srv_key, srv_csr = str(d / "s.crt"), str(d / "s.key"), str(d / "s.csr")
    cli_crt, cli_key, cli_csr = str(d / "c.crt"), str(d / "c.key"), str(d / "c.csr")

    _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", ca_key, "-out", ca_crt, "-days", "1", "-subj", "/CN=agentconnect-test-ca")
    _run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", srv_key, "-out", srv_csr,
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1")
    _run("openssl", "x509", "-req", "-in", srv_csr, "-CA", ca_crt, "-CAkey", ca_key,
         "-CAcreateserial", "-out", srv_crt, "-days", "1", "-copy_extensions", "copy")
    _run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", cli_key, "-out", cli_csr,
         "-subj", "/CN=agentconnect-router-01")
    _run("openssl", "x509", "-req", "-in", cli_csr, "-CA", ca_crt, "-CAkey", ca_key,
         "-CAcreateserial", "-out", cli_crt, "-days", "1")
    return {"ca": ca_crt, "server_crt": srv_crt, "server_key": srv_key,
            "client_crt": cli_crt, "client_key": cli_key}


@pytest.fixture(scope="module")
def server(certs):
    import ssl

    import uvicorn

    port = _free_port()
    app = create_app(ResidencyManager())
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        ssl_certfile=certs["server_crt"], ssl_keyfile=certs["server_key"],
        ssl_ca_certs=certs["ca"], ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"https://localhost:{port}"
    srv.should_exit = True
    t.join(timeout=5)


def test_valid_client_cert_accepted(server, certs):
    tls = TlsClientConfig(
        mode="mutual", ca_cert=certs["ca"],
        client_cert=certs["client_crt"], client_key=certs["client_key"],
    )
    client = HttpLocalClient(server, tls=tls)
    status = client.status()
    assert status.node_id  # handshake succeeded, request served


def test_missing_client_cert_rejected(server, certs):
    import httpx

    # Verify the server cert but present NO client cert -> handshake must fail.
    client = httpx.Client(base_url=server, verify=certs["ca"], timeout=5.0)
    with pytest.raises(httpx.HTTPError):
        client.get("/status")


def test_client_identity_middleware_allowlist():
    from fastapi.testclient import TestClient

    app = create_app(ResidencyManager(), allowed_clients={"agentconnect-router-01"})
    c = TestClient(app)
    # No identity header + no ASGI TLS extension -> deferred to transport (allowed here).
    assert c.get("/status").status_code == 200
    # A trusted-proxy identity header that is NOT allowlisted -> 403.
    assert c.get("/status", headers={"X-Client-Cert-DN": "intruder"}).status_code == 403
    # An allowlisted identity -> 200.
    assert c.get("/status", headers={"X-Client-Cert-DN": "agentconnect-router-01"}).status_code == 200
