"""Herdr live-terminal provider — the same seam, coded against Herdr's surface.

Herdr's described control surface mirrors the tmux mapping exactly:

    Herdr workspace  =  tmux session  =  AgentConnect workspace
    Herdr tab        =  tmux window   =  AgentConnect task
    Herdr pane       =  tmux pane     =  AgentConnect agent session
    Herdr attach     =  tmux attach

so this adapter targets the identical `AgentObservabilityProvider` methods the
tmux provider does. It is written against Herdr's *control socket* protocol
(workspace/tab/pane create + attach URL), NOT faked.

**It is feature-flagged OFF and refuses to pretend.** Herdr is not installable
on this host (established by the lead: no binary, no PyPI, no repo), so there is
no control socket to talk to. `enabled=False` (the default) makes every method a
disabled no-op that reports *why*; constructing it with `enabled=True` while no
socket answers raises, rather than silently degrading to a stub that looks live.

To enable once a Herdr control socket exists (see
docs/adr/0002-herdr-observability-provider.md):

    export AGENTCONNECT_OBSERVABILITY=structured_log,herdr
    export AGENTCONNECT_OBSERVABILITY_HERDR_ENABLED=1
    export AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET=/run/herdr/control.sock
    # then implement `_HerdrControlClient.request()` against the real protocol.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from ..model import (
    AttachInformation,
    CapturedOutput,
    ObservationHandle,
    ObservationOutcome,
    ProviderHealth,
    SessionObservationRequest,
    SpawnObservationRequest,
)
from ..provider import AgentObservabilityProvider

_log = logging.getLogger(__name__)

HERDR_DISABLED_DETAIL = (
    "Herdr provider is disabled: no Herdr binary/control-socket is installable on "
    "this host. Set AGENTCONNECT_OBSERVABILITY_HERDR_ENABLED=1 with a real "
    "AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET once one exists "
    "(see docs/adr/0002-herdr-observability-provider.md)."
)


class HerdrControlError(RuntimeError):
    pass


class _HerdrControlClient:
    """Thin client for Herdr's control socket.

    The method *shapes* below are the real seam an enabled provider would call —
    `workspace()`, `tab()`, `pane()`, `attach_url()`, `capture()`, `kill()` — each
    of which maps one-to-one onto the tmux provider's operations. The transport
    (`request`) is deliberately unimplemented: wiring it to Herdr's actual JSON
    control protocol is the single step gated behind a real socket. Leaving it
    `NotImplementedError` guarantees the provider cannot *appear* to work.
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def request(self, method: str, params: dict) -> dict:  # pragma: no cover - no host support
        raise NotImplementedError(
            "Herdr control-socket transport is not implemented: no Herdr is "
            "installable on this host. Implement against the real protocol to enable."
        )

    def ping(self) -> None:
        self.request("ping", {})


class HerdrObservabilityProvider(AgentObservabilityProvider):
    name = "herdr"

    def __init__(self, enabled: bool = False, socket_path: Optional[str] = None) -> None:
        self.enabled = enabled
        self.socket_path = socket_path or os.environ.get(
            "AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET", ""
        )
        self._client: Optional[_HerdrControlClient] = None
        if self.enabled:
            # Fail loudly, do not degrade to a stub: an operator who set the flag
            # must know the socket is missing rather than believe Herdr is live.
            if not self.socket_path:
                raise HerdrControlError(
                    "Herdr provider enabled but AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET "
                    "is unset; nothing to connect to."
                )
            self._client = _HerdrControlClient(self.socket_path)
            self._client.ping()  # raises NotImplementedError until the protocol exists

    # ---------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        if not self.enabled:
            return ProviderHealth(provider=self.name, available=False,
                                  detail=HERDR_DISABLED_DETAIL,
                                  metadata={"enabled": False})
        try:
            self._client.ping()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(provider=self.name, available=False,
                                  detail=f"herdr control socket unreachable: {exc}",
                                  metadata={"enabled": True, "socket": self.socket_path})
        return ProviderHealth(provider=self.name, available=True,
                              detail=f"herdr control socket {self.socket_path}",
                              metadata={"enabled": True, "socket": self.socket_path})

    # ---------------------------------------------------------------- begin
    def create_session(self, request: SessionObservationRequest) -> ObservationHandle:
        if not self.enabled:
            return self._disabled_handle("session", request.session_id, request)
        # Real seam (unreachable until the transport exists): create the Herdr
        # workspace/tab/pane triple that mirrors tmux session/window/pane.
        ws = self._client.request("workspace.ensure",  # type: ignore[union-attr]
                                  {"id": request.workspace_id or request.task_id})
        tab = self._client.request("tab.ensure",  # type: ignore[union-attr]
                                   {"workspace": ws["id"], "id": request.task_id})
        pane = self._client.request("pane.create",  # type: ignore[union-attr]
                                    {"tab": tab["id"], "command": request.command,
                                     "title": request.title})
        return ObservationHandle(
            provider=self.name, handle_id=pane["id"], kind="session",
            target=f"{ws['id']}/{tab['id']}/{pane['id']}",
            delegation_id=request.delegation_id, trace_id=request.trace_id,
            task_id=request.task_id,
        )

    def spawn_process(self, request: SpawnObservationRequest) -> ObservationHandle:
        if not self.enabled:
            anchor = request.run_id or request.subtask_id or "worker"
            return self._disabled_handle("process", anchor, request)
        ws = self._client.request("workspace.ensure",  # type: ignore[union-attr]
                                  {"id": request.workspace_id or request.task_id})
        tab = self._client.request("tab.ensure",  # type: ignore[union-attr]
                                   {"workspace": ws["id"], "id": request.task_id})
        pane = self._client.request("pane.create",  # type: ignore[union-attr]
                                    {"tab": tab["id"], "command": request.command,
                                     "title": request.title})
        return ObservationHandle(
            provider=self.name, handle_id=pane["id"], kind="process",
            target=f"{ws['id']}/{tab['id']}/{pane['id']}",
            delegation_id=request.delegation_id, trace_id=request.trace_id,
            task_id=request.task_id,
        )

    def _disabled_handle(self, kind, anchor, request) -> ObservationHandle:
        return ObservationHandle(
            provider=self.name, handle_id=f"herdr-disabled:{anchor}", kind=kind,
            target="", delegation_id=getattr(request, "delegation_id", None),
            trace_id=request.trace_id, task_id=request.task_id,
            metadata={"disabled": True, "detail": HERDR_DISABLED_DETAIL},
        )

    # ---------------------------------------------------------------- attach
    def attach_info(self, handle: ObservationHandle) -> AttachInformation:
        if not self.enabled or not handle.target:
            return AttachInformation(provider=self.name, available=False,
                                     detail=HERDR_DISABLED_DETAIL)
        info = self._client.request("pane.attach_url", {"pane": handle.handle_id})  # type: ignore[union-attr]
        return AttachInformation(
            provider=self.name, available=True,
            attach_command=info.get("attach_command", ""),
            read_only_command=info.get("read_only_command", ""),
            detail=info.get("url", ""),
        )

    def capture_output(self, handle: ObservationHandle, max_lines: int = 200) -> CapturedOutput:
        if not self.enabled:
            return CapturedOutput(provider=self.name, handle_id=handle.handle_id,
                                  detail=HERDR_DISABLED_DETAIL)
        res = self._client.request("pane.capture",  # type: ignore[union-attr]
                                   {"pane": handle.handle_id, "lines": max_lines})
        return CapturedOutput(provider=self.name, handle_id=handle.handle_id,
                              lines=res.get("lines", []), truncated=res.get("truncated", False))

    # ----------------------------------------------------------------- close
    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        if not self.enabled or not handle.target:
            return
        self._client.request("pane.kill",  # type: ignore[union-attr]
                             {"pane": handle.handle_id, "outcome": outcome.value})
