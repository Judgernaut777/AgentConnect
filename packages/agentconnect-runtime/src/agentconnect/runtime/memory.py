"""Outbound memory seam for the worker runtime.

A worker can **write** findings to durable memory (e.g. WikiBrain's ``brain_capture``)
but never reads memory back — recall is the *manager's* job. Memory flows one way, up
the trust gradient: capturing what a worker already holds leaks nothing, so even a
lower-tier / remote worker may contribute, while a privileged recall never reaches an
untrusted worker (there is simply no recall path here).

The runtime depends only on the :class:`MemorySink` protocol. The concrete MCP-client
sink (:class:`McpStdioMemorySink`) lives here behind the ``[memory]`` extra and is
injectable, so offline tests use a fake and one-shot/no-memory deployments wire nothing.
"""

from __future__ import annotations

import json
from typing import Optional, Protocol


class MemorySink(Protocol):
    def capture(self, text: str, *, provenance: dict) -> str:
        """Record a durable finding. Returns a short status string. Must NOT raise
        into the loop — on failure return an ``"ERROR: ..."`` string, exactly like a
        tool observation, so a memory outage never breaks task execution."""
        ...


class NullMemorySink:
    """Default when no memory is wired: ``capture`` is a no-op that says so."""

    def capture(self, text: str, *, provenance: dict) -> str:  # noqa: ARG002
        return "ERROR: memory is not configured for this worker."


class McpStdioMemorySink:
    """A :class:`MemorySink` backed by an MCP server exposing ``brain_capture`` over
    stdio (WikiBrain run with ``--contribute-only``). Write-only by construction: it
    only ever calls ``brain_capture``, never a recall tool.

    The MCP client is async; the runtime loop is sync. We hold ONE persistent session
    on a private background event loop and bridge each ``capture`` synchronously, so a
    long-lived worker pays the subprocess/handshake cost once. Needs the ``[memory]``
    extra (the ``mcp`` client). Failures are returned as ``"ERROR: ..."`` strings.
    """

    def __init__(
        self,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        *,
        harness: str = "agentconnect",
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ):
        if command is None:
            # BrainConnect is WikiBrain renamed; during the transition either CLI
            # may be the one installed. Prefer the new name, fall back to the old.
            import shutil

            command = "brainconnect" if shutil.which("brainconnect") else "wiki"
        self._command = command
        self._args = args if args is not None else ["mcp", "serve", "--contribute-only"]
        self._harness = harness
        self._cwd = cwd
        self._timeout = timeout
        self._loop = None
        self._thread = None
        self._stack = None
        self._session = None

    # -- lifecycle (lazy: the loop/subprocess start on first capture) ----------
    def _ensure_loop(self):
        import asyncio
        import threading

        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    async def _ensure_session(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if self._session is not None:
            return self._session
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self._command, args=self._args, cwd=self._cwd)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        return session

    async def _capture_async(self, text: str, provenance: dict) -> str:
        session = await self._ensure_session()
        # Provenance rides in the captured text (task_id, privacy_class, agent_type) so
        # the manager can judge sensitivity at recall time; brain_capture stores it as
        # unvetted pending material behind WikiBrain's human gate.
        payload = f"{text}\n\n[provenance: {json.dumps(provenance, sort_keys=True)}]"
        result = await session.call_tool("brain_capture", {"text": payload, "harness": self._harness})
        parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        return "\n".join(parts) if parts else "captured"

    def capture(self, text: str, *, provenance: dict) -> str:
        import asyncio

        try:
            self._ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(
                self._capture_async(text, provenance), self._loop
            )
            return fut.result(timeout=self._timeout)
        except Exception as exc:  # never break the loop on a memory outage
            return f"ERROR: memory capture failed: {exc}"

    def close(self) -> None:
        import asyncio

        if self._loop is None:
            return
        if self._stack is not None:
            async def _aclose():
                await self._stack.aclose()
            try:
                asyncio.run_coroutine_threadsafe(_aclose(), self._loop).result(timeout=self._timeout)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._session = None
        self._stack = None
