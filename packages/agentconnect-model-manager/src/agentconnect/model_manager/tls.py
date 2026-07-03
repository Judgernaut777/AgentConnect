"""mTLS helpers for the Local Model Manager (handoff §7, Goal 1).

Two layers of protection replace the old shared bearer token:

1. **Authentication (transport).** uvicorn is launched with
   ``ssl_cert_reqs=CERT_REQUIRED`` + ``ssl_ca_certs=<internal CA>``, so the TLS
   handshake fails for any client whose certificate is not signed by the trusted
   internal CA. This is the real replacement for the bearer token — mint router
   client certs only from a CA that issues exclusively to authorized routers.

2. **Per-identity allowlist (application).** :class:`ClientIdentityMiddleware`
   matches the peer certificate's Subject CN / SAN against an allowlist. NOTE:
   uvicorn does not yet surface the peer certificate to the ASGI scope, so the
   identity is read from the ASGI-TLS extension when present, otherwise from a
   trusted reverse-proxy header (``X-Client-Cert-DN`` / ``X-SPIFFE-ID``). With
   pure uvicorn and no proxy, the effective identity boundary is CA issuance and
   this middleware is a defense-in-depth no-op unless a proxy populates the header.
"""

from __future__ import annotations

import dataclasses
import os
import ssl
from typing import Optional

# Moved to core (pure ASGI, shared with the worker transport); re-exported so
# existing imports from this module keep working.
from agentconnect.common.asgi_identity import (  # noqa: F401
    ClientIdentityMiddleware,
    _peer_identity,
)


@dataclasses.dataclass(frozen=True)
class ManagerTlsConfig:
    mode: str = "mutual"  # mutual | insecure_localhost
    cert: Optional[str] = None
    key: Optional[str] = None
    ca: Optional[str] = None


def manager_tls_from_env() -> ManagerTlsConfig:
    return ManagerTlsConfig(
        mode=os.environ.get("MODEL_MANAGER_TLS_MODE", "mutual"),
        cert=os.environ.get("MODEL_MANAGER_TLS_CERT"),
        key=os.environ.get("MODEL_MANAGER_TLS_KEY"),
        ca=os.environ.get("MODEL_MANAGER_TLS_CA"),
    )


def allowed_clients_from_env() -> Optional[set[str]]:
    """Parse ``MODEL_MANAGER_ALLOWED_CLIENTS`` (comma-separated identities) or a
    file path (one identity per line). Returns None when unset (no app-layer
    allowlist; rely on CA issuance)."""
    raw = os.environ.get("MODEL_MANAGER_ALLOWED_CLIENTS")
    if not raw:
        return None
    if os.path.exists(raw):
        with open(raw, "r", encoding="utf-8") as fh:
            return {line.strip() for line in fh if line.strip()}
    return {item.strip() for item in raw.split(",") if item.strip()}


def build_ssl_kwargs(tls: ManagerTlsConfig) -> dict:
    """uvicorn ssl_* kwargs for mutual TLS. Empty dict for insecure_localhost."""
    if tls.mode != "mutual":
        return {}
    missing = [n for n, v in (("cert", tls.cert), ("key", tls.key), ("ca", tls.ca)) if not v]
    if missing:
        raise RuntimeError(
            f"mutual TLS requires MODEL_MANAGER_TLS_{'/'.join(m.upper() for m in missing)}; "
            f"set them or use MODEL_MANAGER_TLS_MODE=insecure_localhost for dev."
        )
    return {
        "ssl_certfile": tls.cert,
        "ssl_keyfile": tls.key,
        "ssl_ca_certs": tls.ca,
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }
