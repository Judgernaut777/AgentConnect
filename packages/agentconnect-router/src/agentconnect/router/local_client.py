"""Client interface to the Local Model Manager (handoff §5).

The router needs deterministic visibility into local inference status. It talks
to the Local Model Manager through this interface, which has two implementations:

  * :class:`InProcessLocalClient` — wraps a :class:`ResidencyManager` directly.
    Used in tests and single-box deployments; no network, fully deterministic.
  * :class:`HttpLocalClient` — calls the Model Manager's HTTP API over **mutual
    TLS**. The router presents a client certificate and verifies the manager's
    server certificate against a private CA. Identity is the certificate — there
    is NO shared application secret on the wire (handoff §7).
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Optional

from ..common.schemas import (
    CanAcceptRequest,
    CanAcceptResponse,
    GenerateRequest,
    GenerateResponse,
    LoadRequest,
    LoadResponse,
    ManagerStatus,
)

if TYPE_CHECKING:
    from ..common.config import TlsClientConfig


class LocalClient(abc.ABC):
    @abc.abstractmethod
    def status(self) -> ManagerStatus: ...

    @abc.abstractmethod
    def can_accept(self, req: CanAcceptRequest) -> CanAcceptResponse: ...

    @abc.abstractmethod
    def generate(self, req: GenerateRequest) -> GenerateResponse: ...

    @abc.abstractmethod
    def load(self, req: LoadRequest) -> LoadResponse: ...


class InProcessLocalClient(LocalClient):
    def __init__(self, manager):  # manager: ResidencyManager
        self._m = manager

    def status(self) -> ManagerStatus:
        return self._m.status()

    def can_accept(self, req: CanAcceptRequest) -> CanAcceptResponse:
        return self._m.can_accept(req)

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        return self._m.generate(req)

    def load(self, req: LoadRequest) -> LoadResponse:
        return self._m.load(req)


class HttpLocalClient(LocalClient):
    """Talks to a remote Local Model Manager over mutual TLS.

    Authentication is the X.509 client certificate: the router presents
    ``(client_cert, client_key)`` and pins the manager's server cert to the
    private CA (``verify=ca_cert``). No shared secret is exchanged. When ``tls``
    is ``None`` or ``mode == "insecure_localhost"`` the client falls back to plain
    HTTP (dev/single-box loopback only)."""

    def __init__(
        self,
        base_url: str,
        *,
        tls: Optional["TlsClientConfig"] = None,
        timeout: float = 30.0,
    ):
        import httpx

        self._base = base_url.rstrip("/")
        if tls is not None and tls.mode == "mutual":
            # Build an explicit SSLContext: pin the manager's server cert to the
            # private CA and present the router's client cert. (Avoids httpx's
            # deprecated verify=<str> / cert=<tuple> shortcuts.)
            import ssl

            ctx = (
                ssl.create_default_context(cafile=tls.ca_cert)
                if tls.ca_cert
                else ssl.create_default_context()
            )
            if tls.client_cert and tls.client_key:
                ctx.load_cert_chain(certfile=tls.client_cert, keyfile=tls.client_key)
            self._client = httpx.Client(base_url=self._base, verify=ctx, timeout=timeout)
        else:
            # insecure_localhost / no TLS material — plain HTTP, loopback only.
            self._client = httpx.Client(base_url=self._base, timeout=timeout)

    def status(self) -> ManagerStatus:
        r = self._client.get("/status")
        r.raise_for_status()
        return ManagerStatus.model_validate(r.json())

    def can_accept(self, req: CanAcceptRequest) -> CanAcceptResponse:
        r = self._client.post("/can_accept", json=req.model_dump(mode="json"))
        r.raise_for_status()
        return CanAcceptResponse.model_validate(r.json())

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        r = self._client.post("/generate", json=req.model_dump(mode="json"))
        r.raise_for_status()
        return GenerateResponse.model_validate(r.json())

    def load(self, req: LoadRequest) -> LoadResponse:
        r = self._client.post("/load", json=req.model_dump(mode="json"))
        r.raise_for_status()
        return LoadResponse.model_validate(r.json())
