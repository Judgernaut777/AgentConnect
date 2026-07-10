import sys
from pathlib import Path

# Make the packages importable from a source checkout without installing.
# PEP 420 namespace packages: all `agentconnect/` dirs on sys.path merge into
# one `agentconnect` namespace, so `agentconnect.common`, `agentconnect.core`,
# `agentconnect.router`, `agentconnect.model_manager`, `agentconnect.runtime`, and the
# backplane adapters (`agentconnect.api`, `.cli`, `.mcp`, `.linear`) all resolve.
ROOT = Path(__file__).resolve().parents[1]
for _pkg in (
    "agentconnect-core",
    "agentconnect-router",
    "agentconnect-model-manager",
    "agentconnect-runtime",
    "agentconnect-api",
    "agentconnect-cli",
    "agentconnect-mcp",
    "agentconnect-linear",
    "agentconnect-temporal",
):
    _src = ROOT / "packages" / _pkg / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


def operator_client(service, linear_sync=None, actor: str = "operator"):
    """A `TestClient` carrying an operator token.

    Every route except `GET /health` now authenticates, so a test that wants to
    drive the API must hold a credential exactly as a caller does. Minting one here
    rather than bypassing `enforce` is the point: the tests exercise the real door.
    """
    from fastapi.testclient import TestClient

    from agentconnect.api.app import create_app

    token = service.mint_operator_token(actor)
    client = TestClient(create_app(service=service, linear_sync=linear_sync))
    client.headers.update({"Authorization": f"Bearer {token.plaintext}"})
    return client
