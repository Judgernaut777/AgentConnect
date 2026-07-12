"""The `AgentObservabilityProvider` seam (production handoff Part II).

AgentConnect owns this interface; a provider implements it. The contract is
deliberately small and total, so a provider can be as thin as a JSONL writer or
as rich as a live terminal multiplexer, and the emitter never needs to know
which it is talking to.

The lifecycle a provider observes:

    create_session / spawn_process   -> ObservationHandle   (begin observing)
    update_state / append_event      -> (ongoing)           (report progress)
    attach_info                      -> AttachInformation   (how a human joins)
    close                            -> (end observing)

Every method has a safe default here, so a partial provider overrides only what
it supports. A provider that offers no live surface (JSONL, OTLP) still answers
`attach_info` — it just reports `available=False`.
"""

from __future__ import annotations

from .model import (
    AgentObservationEvent,
    AttachInformation,
    CapturedOutput,
    ObservationHandle,
    ObservationOutcome,
    ProviderHealth,
    SessionObservationRequest,
    SpawnObservationRequest,
    StateObservationRequest,
)


class AgentObservabilityProvider:
    """Base class for every observability provider.

    Subclasses set ``name`` and override the methods they support. The defaults
    are non-raising no-ops that produce inert handles/attach-info, so the
    composite can treat every provider uniformly.
    """

    name: str = "abstract"

    # ---------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True)

    # ---------------------------------------------------------------- begin
    def create_session(self, request: SessionObservationRequest) -> ObservationHandle:
        return ObservationHandle(
            provider=self.name,
            handle_id=f"{self.name}:{request.session_id}",
            kind="session",
            delegation_id=request.delegation_id,
            trace_id=request.trace_id,
            task_id=request.task_id,
        )

    def spawn_process(self, request: SpawnObservationRequest) -> ObservationHandle:
        anchor = request.run_id or request.subtask_id or request.trace_id
        return ObservationHandle(
            provider=self.name,
            handle_id=f"{self.name}:{anchor}",
            kind="process",
            delegation_id=request.delegation_id,
            trace_id=request.trace_id,
            task_id=request.task_id,
        )

    # ---------------------------------------------------------------- report
    def update_state(self, request: StateObservationRequest) -> None:
        return None

    def append_event(self, event: AgentObservationEvent) -> None:
        return None

    # ---------------------------------------------------------------- attach
    def attach_info(self, handle: ObservationHandle) -> AttachInformation:
        return AttachInformation(
            provider=self.name, available=False,
            detail=f"{self.name} offers no live attach surface",
        )

    def capture_output(self, handle: ObservationHandle, max_lines: int = 200) -> CapturedOutput:
        """Bounded output. A provider with no terminal returns an empty capture."""
        return CapturedOutput(
            provider=self.name, handle_id=handle.handle_id,
            detail=f"{self.name} captures no terminal output",
        )

    # ----------------------------------------------------------------- end
    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        return None
