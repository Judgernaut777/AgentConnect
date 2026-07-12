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
class ToolDecision:
    """AgentConnect's view of a ToolConnect decision.

    ``allowed`` is the only thing that lets a tool run, and it is ``True`` **only** when
    the server explicitly said so. ``default_deny`` distinguishes "a rule forbade this"
    from "no rule matched" (very likely a missing policy); ``unavailable`` marks a deny
    produced because the engine could not be reached or understood, not because it ruled.
    """

    allowed: bool
    reason: str = ""
    decision_id: str = ""
    determining_policies: tuple[str, ...] = ()
    default_deny: bool = False
    unavailable: bool = False
    contract_version: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def deny(cls, reason: str, *, unavailable: bool = False) -> "ToolDecision":
        return cls(allowed=False, reason=reason, default_deny=True, unavailable=unavailable)

    @classmethod
    def from_body(cls, body: Mapping[str, Any]) -> "ToolDecision":
        # Fail closed: anything we cannot read as an explicit allow is a deny.
        return cls(
            allowed=bool(body.get("allowed", False)),
            reason=str(body.get("reason", "")),
            decision_id=str(body.get("decision_id", "")),
            determining_policies=tuple(str(p) for p in (body.get("determining_policies") or ())),
            default_deny=bool(body.get("default_deny", False)),
            contract_version=str(body.get("contract_version", "")),
            raw=dict(body),
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
    ``advisory``); ToolConnect enforces the semantics of whichever is chosen.
    """

    mode: str

    def authorize(
        self, principal: Mapping[str, Any], source_id: str, name: str,
        context: Optional[Mapping[str, Any]] = None,
    ) -> ToolDecision: ...

    def record(
        self, decision_id: str, outcome: str, detail: Optional[Mapping[str, Any]] = None
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
        #: ``required`` (an outage is a denial) or ``advisory`` (a caller may fall back to
        #: a cached pack). The *client* never fabricates an allow in either mode; the mode
        #: only tells a caller what an ``unavailable`` deny means for the workflow.
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
        context: Optional[Mapping[str, Any]] = None,
    ) -> ToolDecision:
        """Ask whether ``principal`` may call ``(source_id, name)``.

        A *deny* is a normal return value with ``allowed=False``. Only genuine
        unavailability — a transport failure, a non-200, a body we cannot read, or an
        incompatible contract MAJOR — resolves to a fail-closed deny carrying
        ``unavailable=True``. There is no path that returns ``allowed=True`` on failure.
        """
        body = {"principal": dict(principal), "source_id": source_id, "name": name}
        if context is not None:
            body["context"] = dict(context)
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
        return decision

    def record(
        self, decision_id: str, outcome: str, detail: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        """Close the loop on an issued decision (contract §3: ``record()``).

        Recording is best-effort audit, not a gate: an unreachable server returns
        ``{"recorded": False, ...}`` rather than raising, so a completed tool run is never
        turned into a crash by an outage on the audit path.
        """
        body: dict[str, Any] = {"outcome": outcome}
        if detail is not None:
            body["detail"] = dict(detail)
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
