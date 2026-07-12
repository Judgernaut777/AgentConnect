"""The real live-terminal observability provider (production handoff Part II).

This is the concrete production-equivalent of the "Herdr" role. Herdr is not
installable on this host, so AgentConnect ships a provider backed by tmux — which
*is* real: real PTYs, real processes, real attach/detach, real bounded capture.

The mapping (spec Part II):

    tmux session  =  workspace      (one live surface per task/review workspace)
    tmux window   =  task tab       (one window per task, titled with the task id)
    tmux pane     =  agent session  (one pane per manager/worker/reviewer)

Everything runs on a **dedicated tmux socket** (`-L <socket>`), never the user's
default server, so observing agents can never collide with a human's own tmux and
`close()` can never kill a window the operator is using elsewhere.

A pane is a real child process. `create_session` runs a command (or an idle
`sh`) in a new pane; `attach_info` returns the exact `tmux -L … attach` command a
human runs to watch it; `capture_output` reads the pane's scrollback with a hard
line bound; `close` kills the pane (and the window when it was the last pane).
"""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import subprocess
import threading
from typing import Optional

from ..model import (
    AgentObservationEvent,
    AttachInformation,
    CapturedOutput,
    ObservationHandle,
    ObservationOutcome,
    ObservationState,
    ProviderHealth,
    SessionObservationRequest,
    SpawnObservationRequest,
    StateObservationRequest,
)
from ..provider import AgentObservabilityProvider

_log = logging.getLogger(__name__)

DEFAULT_SOCKET = "agentconnect-obs"

#: tmux names may not contain '.' or ':' (they are target syntax) — sanitize ids.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name or "unknown")


class TmuxError(RuntimeError):
    pass


class TmuxObservabilityProvider(AgentObservabilityProvider):
    name = "tmux"

    def __init__(
        self,
        socket: str = DEFAULT_SOCKET,
        tmux_bin: Optional[str] = None,
        idle_command: str = "sh -c 'while :; do sleep 3600; done'",
        redactor: Optional[object] = None,
    ) -> None:
        self.socket = socket
        self.tmux_bin = tmux_bin or shutil.which("tmux") or "tmux"
        self.idle_command = idle_command
        #: Optional callable `(text) -> (text, bool_redacted)` for bounded output.
        #: Injected by the emitter so capture goes through AgentConnect's safety
        #: layer rather than this provider re-implementing redaction.
        self.redactor = redactor
        self._lock = threading.RLock()

    # ------------------------------------------------------------- tmux glue
    def _run(self, *args: str, check: bool = True, timeout: float = 10.0) -> subprocess.CompletedProcess:
        cmd = [self.tmux_bin, "-L", self.socket, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise TmuxError(f"{' '.join(cmd)} -> rc={proc.returncode}: {proc.stderr.strip()}")
        return proc

    def _session_exists(self, session: str) -> bool:
        return self._run("has-session", "-t", session, check=False).returncode == 0

    def _window_target(self, session: str, window: str) -> Optional[str]:
        proc = self._run("list-windows", "-t", session, "-F", "#{window_name}", check=False)
        if proc.returncode != 0:
            return None
        if window in proc.stdout.split():
            return f"{session}:{window}"
        return None

    # ---------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        if not shutil.which(self.tmux_bin) and not shutil.which("tmux"):
            return ProviderHealth(provider=self.name, available=False,
                                  detail="tmux binary not found on PATH")
        try:
            proc = subprocess.run([self.tmux_bin, "-V"], capture_output=True, text=True, timeout=5)
            version = proc.stdout.strip() or proc.stderr.strip()
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(provider=self.name, available=False, detail=str(exc))
        return ProviderHealth(
            provider=self.name, available=True, detail=version,
            metadata={"socket": self.socket, "version": version},
        )

    # ------------------------------------------------------------- internals
    def _ensure_pane(
        self, session: str, window: str, pane_title: str, command: str,
    ) -> str:
        """Return a stable pane target `session:window.{pane_id}`.

        Creates the session (workspace) and window (task tab) on first use, then
        adds a pane (agent) running `command`. Uses tmux's own unique `pane_id`
        (`%N`) as the durable anchor, so later splits never shift the target.
        """
        run_cmd = command.strip() or self.idle_command
        with self._lock:
            if not self._session_exists(session):
                # New detached server-side session; first window is our task tab.
                self._run(
                    "new-session", "-d", "-s", session, "-n", window,
                    "-x", "200", "-y", "50", run_cmd,
                )
                # This tmux server is dedicated to observability, so keep panes
                # around after their command exits (remain-on-exit): an agent whose
                # process finishes leaves a readable dead pane instead of destroying
                # the window — and the server never exits out from under us.
                self._run("set-option", "-g", "remain-on-exit", "on", check=False)
                pane_id = self._run(
                    "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}",
                ).stdout.split()[0]
            else:
                if self._window_target(session, window) is None:
                    self._run("new-window", "-d", "-t", session, "-n", window, run_cmd)
                    pane_id = self._run(
                        "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}",
                    ).stdout.split()[0]
                else:
                    # Split the existing task window to add another agent pane.
                    out = self._run(
                        "split-window", "-d", "-t", f"{session}:{window}",
                        "-P", "-F", "#{pane_id}", run_cmd,
                    ).stdout.strip()
                    pane_id = out or self._run(
                        "list-panes", "-t", f"{session}:{window}", "-F", "#{pane_id}",
                    ).stdout.split()[-1]
                    self._run("select-layout", "-t", f"{session}:{window}", "tiled",
                              check=False)
            # Title the pane with the agent role/id so `attach` shows who is who.
            self._run("select-pane", "-t", pane_id, "-T", pane_title[:60], check=False)
        return f"{session}:{window}.{pane_id}"

    def _handle(
        self, kind: str, target: str, request_provider_ids: dict,
    ) -> ObservationHandle:
        return ObservationHandle(
            provider=self.name, handle_id=target, kind=kind, target=target,
            **request_provider_ids,
        )

    # ---------------------------------------------------------------- begin
    def create_session(self, request: SessionObservationRequest) -> ObservationHandle:
        session = _safe(request.workspace_id or request.task_id or "workspace")
        window = _safe(request.task_id or request.session_id or "task")
        title = request.title or f"{request.agent_role}:{request.agent_id}"
        target = self._ensure_pane(session, window, title, request.command)
        return self._handle("session", target, {
            "delegation_id": request.delegation_id, "trace_id": request.trace_id,
            "task_id": request.task_id,
            "metadata": {"session_id": request.session_id, "agent_id": request.agent_id,
                         "agent_role": request.agent_role, "socket": self.socket,
                         "workspace": session, "window": window},
        })

    def spawn_process(self, request: SpawnObservationRequest) -> ObservationHandle:
        session = _safe(request.workspace_id or request.task_id or "workspace")
        window = _safe(request.task_id or "task")
        anchor = request.run_id or request.subtask_id or "worker"
        title = request.title or f"{request.agent_role}:{anchor}"
        target = self._ensure_pane(session, window, title, request.command)
        return self._handle("process", target, {
            "delegation_id": request.delegation_id, "trace_id": request.trace_id,
            "task_id": request.task_id,
            "metadata": {"subtask_id": request.subtask_id, "run_id": request.run_id,
                         "agent_id": request.agent_id, "agent_role": request.agent_role,
                         "socket": self.socket, "workspace": session, "window": window},
        })

    # ---------------------------------------------------------------- report
    def update_state(self, request: StateObservationRequest) -> None:
        target = request.handle.target
        if not target:
            return
        label = request.provider_state or request.state.value
        try:
            self._run("select-pane", "-t", target, "-T",
                      f"[{label}] {request.detail}"[:60], check=False)
        except Exception as exc:  # noqa: BLE001 — a title is cosmetic
            _log.debug("tmux update_state title failed: %s", exc)

    def append_event(self, event: AgentObservationEvent) -> None:
        # A live provider does not replay every event into the pane — that would
        # corrupt the agent's own terminal. State is surfaced via pane titles
        # (update_state) and the durable JSONL provider holds the full stream.
        return None

    # ---------------------------------------------------------------- attach
    def attach_info(self, handle: ObservationHandle) -> AttachInformation:
        target = handle.target
        session = target.split(":", 1)[0] if target else ""
        if not target or not self._session_exists(session):
            return AttachInformation(
                provider=self.name, available=False,
                detail=f"no live tmux session for {target!r} on socket {self.socket!r}",
                metadata={"socket": self.socket},
            )
        base = f"{shlex.quote(self.tmux_bin)} -L {shlex.quote(self.socket)}"
        # Read-only: `-r` attaches without letting the viewer send keys.
        return AttachInformation(
            provider=self.name, available=True,
            attach_command=f"{base} attach-session -t {shlex.quote(target)}",
            read_only_command=f"{base} attach-session -r -t {shlex.quote(target)}",
            detail=f"pane {target}",
            metadata={"socket": self.socket, "session": session, "target": target},
        )

    def capture_output(self, handle: ObservationHandle, max_lines: int = 200) -> CapturedOutput:
        target = handle.target
        bound = max(1, min(max_lines, 5000))
        try:
            proc = self._run(
                "capture-pane", "-p", "-t", target, "-S", f"-{bound}", check=False,
            )
        except Exception as exc:  # noqa: BLE001
            return CapturedOutput(provider=self.name, handle_id=handle.handle_id,
                                  detail=f"capture failed: {exc}")
        if proc.returncode != 0:
            return CapturedOutput(provider=self.name, handle_id=handle.handle_id,
                                  detail=proc.stderr.strip() or "pane gone")
        text = proc.stdout
        redacted = False
        if self.redactor is not None:
            try:
                text, redacted = self.redactor(text)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 — never leak raw on redactor error
                _log.warning("output redaction failed, withholding: %s", exc)
                return CapturedOutput(provider=self.name, handle_id=handle.handle_id,
                                      redacted=True, detail=f"redaction failed: {exc}")
        lines = text.splitlines()
        # `capture-pane` returns the whole visible pane, so a mostly-idle pane is
        # padded with trailing blank rows. Drop them, then bound to the last N of
        # what is actually content — otherwise the bound would return only padding.
        while lines and not lines[-1].strip():
            lines.pop()
        truncated = len(lines) > bound
        return CapturedOutput(
            provider=self.name, handle_id=handle.handle_id, lines=lines[-bound:],
            truncated=truncated, redacted=redacted,
        )

    # ----------------------------------------------------------------- close
    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        target = handle.target
        if not target:
            return
        session = target.split(":", 1)[0]
        with self._lock:
            # Mark the pane's fate in its title before killing, so a human who
            # attached mid-run sees why it went away.
            self._run("select-pane", "-t", target, "-T", f"[{outcome.value}]",
                      check=False)
            self._run("kill-pane", "-t", target, check=False)
            # If that was the last pane, the window/session is already gone; if the
            # whole workspace session is now empty, reap it too.
            if not self._run("list-panes", "-t", session, check=False).stdout.strip():
                self._run("kill-session", "-t", session, check=False)

    # ------------------------------------------------------------- teardown
    def kill_server(self) -> None:
        """Tear down the entire dedicated tmux server. For test/demo cleanup —
        never touches the user's default server (different socket)."""
        self._run("kill-server", check=False)
