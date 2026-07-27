"""AgentConnect-owned ToolConnect governance client (Connect contract §6b).

ToolConnect ships its own stdlib client (``toolconnect.client.ToolConnectClient``),
but a shipped AgentConnect *product* cannot import a sibling repo at runtime. So this
module is AgentConnect's **own** thin adapter over ToolConnect's HTTP decision API —
``POST /authorize``, ``POST /decisions/{id}/outcome``, ``GET /health`` — speaking the
same wire shapes, owned here, and depending on nothing from the ToolConnect package.

The posture is the one the contract insists on and that no other AgentConnect adapter
shares: **a missing decision is a denial.** Memory fails open (an absent brain returns
an empty pack, and no workflow fails for want of it); a policy engine that fails open is
not a policy engine — a missing authorization makes an agent *unconstrained*, not merely
dumber. So :meth:`ToolConnectGovernor.authorize` never returns an allow it did not
receive: an unreachable server, a non-200, an unreadable body, or a server announcing an
incompatible decision-contract MAJOR all resolve to a fail-closed deny, and the deny
carries ``unavailable=True`` so the caller can tell a *policy* deny (a rule fired) from an
*outage* deny.

This adapter is **not on the invocation data path.** Like the ToolConnect service itself,
it authorizes and records; it never invokes a tool. There is deliberately no
``invoke``/``call`` method — AgentConnect's worker runtime stays the only thing that runs
anything.

Contract 1.1 (argument-bound one-use grants): ``authorize`` accepts optional keyword-only
``args``/``ttl_seconds``. When ``args`` is supplied and the decision allows, the server
issues a one-use grant bound to the exact canonical-JSON hash of those args; the caller
must then call :meth:`ToolConnectGovernor.redeem` with the SAME final args immediately
before executing the tool. ``authorize`` still never carries the request itself — it asks
"may I", ``redeem`` says "consume that permission now", and the caller executes. A server
that allows but issues no grant when ``args`` was sent (a pre-1.1 server silently dropping
the field) is treated as a fail-closed outage-deny — see the mixed-fleet rule below.
``authorize`` without ``args`` is unchanged: a decision-only response, no grant, exactly
contract 1.0 behavior — this additivity is what makes the 1.0 -> 1.1 bump backward
compatible. ``EXPECTED_CONTRACT_MAJOR`` stays ``"1"``; that is the proof the bump is
additive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

#: The decision-contract MAJOR this adapter was written against. A server announcing a
#: different major in ``contract_version`` is one we cannot safely read, so we fail closed
#: rather than misinterpret a future shape as an allow.
EXPECTED_CONTRACT_MAJOR = "1"


class ToolConnectUnavailable(Exception):
    """Transport failure reaching a ToolConnect decision point.

    Raised only internally by :meth:`ToolConnectGovernor._call`; the public
    :meth:`~ToolConnectGovernor.authorize` catches it and returns a fail-closed deny so
    unavailability can never be mistaken for an allow at a call site.
    """


@dataclass(frozen=True)
class ToolGrant:
    """A one-use, argument-bound grant issued alongside an allow decision.

    Present only when ``authorize`` was called with ``args`` and the decision allowed;
    ``None`` on every deny and on every args-less (contract-1.0-shaped) call. Redeem it
    with the SAME final args, immediately before executing the tool.
    """

    grant_id: str
    args_hash: str = ""
    expires_at: str = ""
    ttl_seconds: int = 0


@dataclass(frozen=True)
class RedeemResult:
    """The outcome of redeeming a grant. ``redeemed`` is ``True`` **only** when the
    server's response carried the literal JSON ``true`` — never inferred, never
    defaulted. ``unavailable`` marks a transport/shape/contract-major failure (as
    opposed to a genuine server-side deny reason like ``args_mismatch``)."""

    redeemed: bool
    reason: str = ""
    grant_id: str = ""
    decision_id: str = ""
    source_id: str = ""
    name: str = ""
    unavailable: bool = False
    contract_version: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDecision:
    """AgentConnect's view of a ToolConnect decision.

    ``allowed`` is the only thing that lets a tool run, and it is ``True`` **only** when
    the server explicitly said so. ``default_deny`` distinguishes "a rule forbade this"
    from "no rule matched" (very likely a missing policy); ``unavailable`` marks a deny
    produced because the engine could not be reached or understood, not because it ruled.
    ``grant`` is populated only when the caller sent ``args`` and the decision allowed
    (contract 1.1); it is ``None`` on every deny and on every args-less call.
    """

    allowed: bool
    reason: str = ""
    decision_id: str = ""
    determining_policies: tuple[str, ...] = ()
    default_deny: bool = False
    unavailable: bool = False
    contract_version: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)
    grant: Optional[ToolGrant] = None

    @classmethod
    def deny(cls, reason: str, *, unavailable: bool = False) -> "ToolDecision":
        return cls(allowed=False, reason=reason, default_deny=True, unavailable=unavailable)

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "ToolDecision":
        # Fail closed: anything we cannot read as an explicit allow is a deny.
        grant_body = body.get("grant")
        grant = None
        if isinstance(grant_body, dict):
            grant = ToolGrant(
                grant_id=str(grant_body.get("grant_id", "")),
                args_hash=str(grant_body.get("args_hash", "")),
                expires_at=str(grant_body.get("expires_at", "")),
                ttl_seconds=int(grant_body.get("ttl_seconds") or 0),
            )
        return cls(
            allowed=bool(body.get("allowed", False)),
            reason=str(body.get("reason", "")),
            decision_id=str(body.get("decision_id", "")),
            determining_policies=tuple(str(p) for p in (body.get("determining_policies") or ())),
            default_deny=bool(body.get("default_deny", False)),
            contract_version=str(body.get("contract_version", "")),
            raw=dict(body),
            grant=grant,
        )


#: The source_id AgentConnect uses when a declared tool is a bare name with no
#: source qualifier. A worker's declared ``tools`` list is names, not namespaced
#: ``(source_id, name)`` identities; the honest default is to attribute them to the
#: worker's harness (passed explicitly by the caller), and this constant is only the
#: fallback for a caller that supplies neither a qualifier nor a source.
DEFAULT_TOOL_SOURCE_ID = "agentconnect"


def split_tool_ref(entry: str, default_source_id: str) -> tuple[str, str]:
    """Parse a declared tool entry into ``(source_id, name)``.

    A worker declares tools as bare names; a caller may also pass a
    ``"source_id:name"`` qualifier to authorize a specific namespaced tool. A bare
    name resolves against ``default_source_id`` (the worker's harness). The name half
    keeps any additional colons, so ``"s:a:b"`` is source ``s`` / name ``a:b``.
    """
    if ":" in entry:
        source_id, name = entry.split(":", 1)
        if source_id and name:
            return source_id, name
    return default_source_id, entry


@dataclass(frozen=True)
class ToolUseAuthorization:
    """The aggregate outcome of authorizing a *set* of declared tools.

    ``allowed`` is ``True`` only when every consulted tool was allowed. The first
    tool that is denied (a policy deny OR an ``unavailable`` outage deny) sets
    ``allowed=False`` and names itself in ``denied_tool`` with its ``decision``, so a
    caller can block and report *which* tool failed and *why*. ``governed`` records
    whether a governor was actually consulted: when no governor is bound the result is
    a permissive ``allowed=True, governed=False`` no-op that preserves standalone
    behavior, and callers must not mistake it for a real allow decision.
    """

    allowed: bool
    governed: bool
    decisions: tuple[tuple[str, str, ToolDecision], ...] = ()
    denied_tool: str = ""
    decision: Optional[ToolDecision] = None

    @property
    def unavailable(self) -> bool:
        """True when the blocking deny was an outage (fail-closed), not a policy rule."""
        return bool(self.decision and self.decision.unavailable)

    def as_metadata(self) -> dict[str, Any]:
        return {
            "governed": self.governed,
            "allowed": self.allowed,
            "denied_tool": self.denied_tool or None,
            "unavailable": self.unavailable,
            "reason": (self.decision.reason[:200] if self.decision else ""),
            "decision_id": (self.decision.decision_id if self.decision else ""),
        }


@runtime_checkable
class ToolGovernor(Protocol):
    """The seam the ToolConnect contract (§3) proposes AgentConnect own.

    Note the absence of ``invoke`` — AgentConnect still calls tools; the governor only
    authorizes and records. ``mode`` is the caller's declared posture (``required`` or
    ``advisory``); today it is recorded with the decision, and enforcement is identical
    in both modes — every deny, outage included, blocks the subtask.

    ``redeem`` is REQUIRED (contract 1.1), not an optional subprotocol: a governor that
    could silently downgrade to decision-only when asked to redeem a grant would recreate
    the exact enforcement gap argument-bound grants exist to close — a final-boundary
    caller finding no usable ``redeem`` must refuse execution, never fall back to treating
    the bare ``authorize`` allow as sufficient. Only two implementers exist in this
    codebase (:class:`ToolConnectGovernor` and the test-only ``FakeGovernor``); both are
    updated in the same change that adds this method to the Protocol.
    """

    mode: str

    def authorize(
        self, principal: Mapping[str, Any], source_id: str, name: str,
        context: Optional[Mapping[str, Any]] = None, *,
        args: Optional[Mapping[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> ToolDecision: ...

    def redeem(
        self, grant_id: str, principal: Mapping[str, Any], args: Mapping[str, Any],
    ) -> RedeemResult: ...

    def record(
        self, decision_id: str, outcome: str, detail: Optional[Mapping[str, Any]] = None,
        *, grant_id: Optional[str] = None,
    ) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...


class ToolConnectGovernor:
    """A thin, fail-closed :class:`ToolGovernor` over ToolConnect's HTTP decision API.

    The transport is injectable — ``(method, url, json) -> (status, body)`` — so the
    fail-closed and wire-mapping logic is testable without a live server, exactly like
    :class:`~agentconnect.core.local_compute.HttpLocalComputeProvider`. With no transport
    injected it uses ``httpx`` (lazy import, as the memory adapters do).
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        mode: str = "required",
        timeout: float = 10.0,
        transport: Optional[Callable[[str, str, Optional[dict]], tuple[int, Any]]] = None,
    ) -> None:
        if not base_url:
            raise ValueError("ToolConnect base_url is required")
        self.base_url = base_url.rstrip("/")
        #: Optional bearer credential, sent verbatim as ``Authorization`` (mirrors the
        #: memory adapters). Never logged — a token in a warning line is a leak.
        self.token = token or None
        #: ``required`` or ``advisory``. No cached-pack fallback is built: in both modes
        #: every deny — outage included — blocks the subtask. The *client* never
        #: fabricates an allow; the mode is recorded so the audit trail shows the
        #: caller's declared posture.
        self.mode = mode if mode in ("required", "advisory") else "required"
        self.timeout = timeout
        self._transport = transport

    # -- transport --------------------------------------------------------------
    def _call(
        self, method: str, path: str, payload: Optional[dict] = None
    ) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        if self._transport is not None:
            return self._transport(method, url, payload)
        import httpx  # lazy: only the network path needs it

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = self.token
        try:
            response = httpx.request(
                method, url, json=payload, headers=headers, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 — every transport failure is unavailability
            raise ToolConnectUnavailable(f"toolconnect unreachable at {url}: {exc}") from exc
        try:
            body = response.json() if response.content else None
        except Exception:  # noqa: BLE001 — an unreadable body is unavailability, not an allow
            body = None
        return response.status_code, body

    # -- decision surface -------------------------------------------------------
    def authorize(
        self, principal: Mapping[str, Any], source_id: str, name: str,
        context: Optional[Mapping[str, Any]] = None, *,
        args: Optional[Mapping[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
    ) -> ToolDecision:
        """Ask whether ``principal`` may call ``(source_id, name)``.

        A *deny* is a normal return value with ``allowed=False``. Only genuine
        unavailability — a transport failure, a non-200, a body we cannot read, or an
        incompatible contract MAJOR — resolves to a fail-closed deny carrying
        ``unavailable=True``. There is no path that returns ``allowed=True`` on failure.

        When ``args`` is given, the request is argument-bound (contract 1.1): the
        server hashes and canonicalizes ``args`` itself — this adapter never computes
        or transmits a hash, only the raw mapping — and, on allow, issues a one-use
        grant that must be redeemed (:meth:`redeem`) with the SAME args immediately
        before execution. If the server allows but issues no grant (a pre-1.1 server
        silently dropping the ``args``/``ttl_seconds`` fields), that is NOT treated as
        a real allow: it is the "mixed-fleet" case, and this method fails closed with
        ``unavailable=True`` rather than let a caller execute ungoverned.
        """
        body = {"principal": dict(principal), "source_id": source_id, "name": name}
        if context is not None:
            body["context"] = dict(context)
        if args is not None:
            body["args"] = dict(args)
            if ttl_seconds is not None:
                body["ttl_seconds"] = ttl_seconds
        try:
            status, payload = self._call("POST", "/authorize", body)
        except ToolConnectUnavailable as exc:
            _log.warning("toolconnect authorize unreachable; denying fail-closed: %s", exc)
            return ToolDecision.deny("toolconnect unreachable", unavailable=True)
        if status != 200 or not isinstance(payload, dict) or "allowed" not in payload:
            _log.warning("toolconnect /authorize returned %s; denying fail-closed", status)
            return ToolDecision.deny(f"toolconnect /authorize returned {status}", unavailable=True)
        decision = ToolDecision.from_body(payload)
        major = (decision.contract_version or "0").split(".", 1)[0]
        if decision.contract_version and major != EXPECTED_CONTRACT_MAJOR:
            _log.warning(
                "toolconnect decision contract v%s incompatible with expected major %s; "
                "denying fail-closed", decision.contract_version, EXPECTED_CONTRACT_MAJOR)
            return ToolDecision.deny(
                f"incompatible decision contract v{decision.contract_version}", unavailable=True)
        if args is not None and decision.allowed and decision.grant is None:
            # Mixed-fleet rule: an allow-without-grant when args were sent means the
            # server did not honor the argument-binding request (most likely a
            # pre-1.1 server that silently dropped the "args" field). Treating that
            # as an ordinary allow would let the caller execute completely
            # ungoverned at the final boundary — refuse instead.
            _log.warning(
                "toolconnect allowed %s:%s but issued no grant despite args being sent "
                "(server pre-1.1?); denying fail-closed", source_id, name)
            return ToolDecision.deny(
                "authorize allowed but issued no grant (server pre-1.1?)", unavailable=True)
        return decision

    def redeem(
        self, grant_id: str, principal: Mapping[str, Any], args: Mapping[str, Any],
    ) -> RedeemResult:
        """Atomically consume a one-use grant immediately before executing the tool.

        Same never-raise, fail-closed posture as :meth:`authorize`: any transport
        failure, non-200, unreadable/missing-``"redeemed"`` body, or incompatible
        contract major resolves to ``RedeemResult(redeemed=False, unavailable=True)`` —
        never an exception, and never inferred as redeemed. ``redeemed`` is ``True``
        only when the server's JSON body carries the literal ``true``.
        """
        body = {"principal": dict(principal), "args": dict(args)}
        try:
            status, payload = self._call("POST", f"/grants/{grant_id}/redeem", body)
        except ToolConnectUnavailable as exc:
            _log.warning("toolconnect redeem(%s) unreachable; denying fail-closed: %s",
                         grant_id, exc)
            return RedeemResult(
                False, reason=f"toolconnect unreachable: {exc}",
                grant_id=grant_id, unavailable=True)
        if status != 200 or not isinstance(payload, dict) or "redeemed" not in payload:
            _log.warning("toolconnect /grants/%s/redeem returned %s; denying fail-closed",
                         grant_id, status)
            return RedeemResult(
                False, reason=f"/redeem returned {status}",
                grant_id=grant_id, unavailable=True)
        cv = str(payload.get("contract_version", ""))
        major = (cv or "0").split(".", 1)[0]
        if cv and major != EXPECTED_CONTRACT_MAJOR:
            _log.warning(
                "toolconnect redeem contract v%s incompatible with expected major %s; "
                "denying fail-closed", cv, EXPECTED_CONTRACT_MAJOR)
            return RedeemResult(
                False, reason=f"incompatible decision contract v{cv}",
                grant_id=grant_id, unavailable=True, contract_version=cv)
        return RedeemResult(
            redeemed=payload.get("redeemed") is True,  # explicit True only, never inferred
            reason=str(payload.get("reason", "")),
            grant_id=grant_id,
            decision_id=str(payload.get("decision_id") or ""),
            source_id=str(payload.get("source_id") or ""),
            name=str(payload.get("name") or ""),
            contract_version=cv,
            raw=dict(payload),
        )

    def record(
        self, decision_id: str, outcome: str, detail: Optional[Mapping[str, Any]] = None,
        *, grant_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Close the loop on an issued decision (contract §3: ``record()``).

        ``grant_id``, when given, is sent as a TOP-LEVEL body field — that is what
        ToolConnect's ``POST /decisions/{id}/outcome`` reads to close the grant in the
        same call (contract 1.1, close-via-outcome). A grant_id tucked inside
        ``detail`` is opaque payload to the server and closes nothing.

        Recording is best-effort audit, not a gate: an unreachable server returns
        ``{"recorded": False, ...}`` rather than raising, so a completed tool run is never
        turned into a crash by an outage on the audit path.
        """
        body: dict[str, Any] = {"outcome": outcome}
        if detail is not None:
            body["detail"] = dict(detail)
        if grant_id is not None:
            body["grant_id"] = grant_id
        try:
            status, payload = self._call("POST", f"/decisions/{decision_id}/outcome", body)
        except ToolConnectUnavailable as exc:
            _log.warning("toolconnect record(%s) unreachable: %s", decision_id, exc)
            return {"recorded": False, "detail": str(exc)}
        if status != 200 or not isinstance(payload, dict):
            _log.warning("toolconnect record(%s) returned %s", decision_id, status)
            return {"recorded": False, "status": status}
        return payload

    def health(self) -> dict[str, Any]:
        try:
            status, payload = self._call("GET", "/health")
        except ToolConnectUnavailable as exc:
            return {"status": "unreachable", "detail": str(exc)}
        if status != 200 or not isinstance(payload, dict):
            return {"status": "unreachable", "detail": f"/health returned {status}"}
        return payload
