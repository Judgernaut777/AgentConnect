"""The emitter: AgentConnect lifecycle -> observation event -> providers.

This is the one object the service holds. Every emission site calls
:meth:`ObservabilityEmitter.observe`; the emitter builds a normalized
:class:`AgentObservationEvent`, deduplicates it, and fans it out to the composite
provider. The live-surface lifecycle (`begin_session`, `begin_process`, `close`)
is here too, so the service never touches a provider directly.

Guarantees the service relies on (Part IV):

* **Emission never raises under the advisory policy.** `observe` wraps the whole
  build-and-fan-out in a guard, so a bug in a provider — or in event
  construction — can never abort the ledger mutation that triggered it. Under
  `task_blocking` the composite re-raises deliberately; the service calls the
  emitter *after* it has durably written the ledger, so even a raised error
  cannot corrupt canonical state, only surface as a task-level failure.
* **Idempotent.** A repeated `event_id` (or `(trace_id, sequence)` pair) is
  dropped before fan-out, so a workflow replay or a retried activity does not
  double-log a transition.
* **Out-of-order tolerant.** Events carry a monotonic `sequence`; readers restore
  order from it. The emitter never assumes calls arrive in order.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from .model import (
    DEFAULT_STATE_FOR_EVENT,
    AgentObservationEvent,
    AttachInformation,
    CapturedOutput,
    EventType,
    ObservationHandle,
    ObservationOutcome,
    ObservationState,
    SessionObservationRequest,
    SpawnObservationRequest,
    StateObservationRequest,
)
from .providers.composite import CompositeObservabilityProvider, FailurePolicy
from .providers.noop import NoopObservabilityProvider

_log = logging.getLogger(__name__)


class ObservabilityEmitter:
    def __init__(
        self,
        provider: Optional[CompositeObservabilityProvider] = None,
        *,
        clock: Callable[[], float] = None,
        redactor: Optional[Callable[[str], tuple]] = None,
    ) -> None:
        self.provider = provider or CompositeObservabilityProvider(
            [NoopObservabilityProvider()]
        )
        import time as _time
        self._clock = clock or _time.time
        self._redactor = redactor
        self._lock = threading.RLock()
        self._seq = 0
        self._seen: set[str] = set()
        #: Bounded, so a long-lived process cannot grow the dedupe set without end.
        self._seen_order: list[str] = []

    @property
    def enabled(self) -> bool:
        """True when at least one non-noop provider is configured."""
        return any(p.name != "noop" for p in self.provider.providers)

    # ---------------------------------------------------------------- dedupe
    def _remember(self, event_id: str) -> bool:
        """Return True if this id is new (and record it); False if a duplicate."""
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > 20000:
            drop = self._seen_order[:10000]
            self._seen_order = self._seen_order[10000:]
            self._seen.difference_update(drop)
        return True

    # ----------------------------------------------------------------- emit
    def emit(self, event: AgentObservationEvent) -> bool:
        """Low-level fan-out with dedupe. Returns False when dropped as a dup.

        Honors a caller-supplied `sequence` (for replay/out-of-order demos); when
        the event carries the sentinel sequence 0 and was built by `observe`, a
        monotonic sequence has already been assigned.
        """
        with self._lock:
            if not self._remember(event.event_id):
                return False
        try:
            self.provider.append_event(event)
        except Exception:  # noqa: BLE001
            if self.provider.policy is FailurePolicy.task_blocking:
                raise
            _log.warning("observability fan-out failed for %s", event.event_id, exc_info=True)
        return True

    def observe(
        self,
        event_type: EventType,
        *,
        trace_id: str,
        event_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        sequence: Optional[int] = None,
        state: Optional[ObservationState] = None,
        outcome: Optional[ObservationOutcome] = None,
        task_id: Optional[str] = None,
        delegation_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        review_id: Optional[str] = None,
        agent_id: str = "unknown",
        agent_role: str = "unknown",
        provider: str = "",
        workspace_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[AgentObservationEvent]:
        """Build, dedupe, and fan out one lifecycle event. Never raises under the
        advisory policy."""
        try:
            with self._lock:
                self._seq += 1
                seq = sequence if sequence is not None else self._seq
            resolved_state = state or DEFAULT_STATE_FOR_EVENT.get(
                event_type, ObservationState.unknown
            )
            anchor = (
                run_id or subtask_id or review_id or session_id or task_id or trace_id
            )
            eid = event_id or dedupe_key or f"{event_type.value}:{anchor}:{seq}"
            event = AgentObservationEvent(
                event_id=eid, sequence=seq, timestamp=self._clock(),
                metadata=self._redact_metadata(metadata or {}),
                event_type=event_type, state=resolved_state, outcome=outcome,
                trace_id=trace_id, task_id=task_id, delegation_id=delegation_id,
                parent_delegation_id=parent_delegation_id, subtask_id=subtask_id,
                session_id=session_id, run_id=run_id, review_id=review_id,
                agent_id=agent_id, agent_role=agent_role, provider=provider,
                workspace_id=workspace_id,
            )
            self.emit(event)
            return event
        except Exception:  # noqa: BLE001 — emission must never break the ledger
            if getattr(self.provider, "policy", None) is FailurePolicy.task_blocking:
                raise
            _log.warning("observe(%s) failed", event_type, exc_info=True)
            return None

    # ------------------------------------------------------- live surface
    def begin_session(self, request: SessionObservationRequest) -> list[ObservationHandle]:
        try:
            return self.provider.create_session(request)
        except Exception:  # noqa: BLE001
            if self.provider.policy is FailurePolicy.task_blocking:
                raise
            _log.warning("begin_session failed", exc_info=True)
            return []

    def begin_process(self, request: SpawnObservationRequest) -> list[ObservationHandle]:
        try:
            return self.provider.spawn_process(request)
        except Exception:  # noqa: BLE001
            if self.provider.policy is FailurePolicy.task_blocking:
                raise
            _log.warning("begin_process failed", exc_info=True)
            return []

    def update_state(
        self, handle: ObservationHandle, state: ObservationState,
        outcome: Optional[ObservationOutcome] = None, detail: str = "",
        provider_state: Optional[str] = None,
    ) -> None:
        req = StateObservationRequest(
            handle=handle, state=state, outcome=outcome, detail=detail,
            provider_state=provider_state,
        )
        try:
            self.provider.update_state(req)
        except Exception:  # noqa: BLE001
            if self.provider.policy is FailurePolicy.task_blocking:
                raise
            _log.warning("update_state failed", exc_info=True)

    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        try:
            self.provider.close(handle, outcome)
        except Exception:  # noqa: BLE001
            if self.provider.policy is FailurePolicy.task_blocking:
                raise
            _log.warning("close failed", exc_info=True)

    def is_live(self, handle: ObservationHandle) -> Optional[bool]:
        """True/False/None whether the observed process is still alive. Never
        raises: a probe error is reported as ``None`` (unknown), not a crash."""
        try:
            return self.provider.is_live(handle)
        except Exception:  # noqa: BLE001
            _log.warning("is_live failed", exc_info=True)
            return None

    def attach_info(self, handle: ObservationHandle) -> AttachInformation:
        return self.provider.attach_info(handle)

    def capture_output(self, handle: ObservationHandle, max_lines: int = 200) -> CapturedOutput:
        return self.provider.capture_output(handle, max_lines=max_lines)

    def redact(self, text: str) -> tuple:
        if self._redactor is None:
            return text, False
        return self._redactor(text)

    #: Metadata keys whose values are always scrubbed regardless of content — a
    #: belt-and-suspenders list so a token that does not match the redactor's
    #: pattern is still never persisted into the event stream.
    _SENSITIVE_KEYS = frozenset({
        "token", "secret", "password", "passwd", "api_key", "apikey",
        "authorization", "auth", "credential", "credentials", "bearer",
        "session_token", "access_token", "refresh_token", "private_key",
    })

    def _redact_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Scrub sensitive values out of event metadata before it is persisted.

        Two layers: (1) any key whose name looks like a credential is masked
        outright; (2) every remaining string value is run through the safety
        redactor (the same one that scrubs terminal output), so a secret that
        landed in an innocuously-named field is still caught. Non-string scalars
        (ids, counts, enums) pass through untouched. Never raises — a redactor
        error masks the value rather than leaking it."""
        if not metadata:
            return {}
        out: dict[str, Any] = {}
        for key, value in metadata.items():
            if str(key).lower() in self._SENSITIVE_KEYS:
                out[key] = "[redacted]"
                continue
            if isinstance(value, str) and self._redactor is not None:
                try:
                    redacted_text, _ = self._redactor(value)
                    out[key] = redacted_text
                except Exception:  # noqa: BLE001 — on scanner failure, mask
                    out[key] = "[redacted: scan failed]"
            else:
                out[key] = value
        return out
