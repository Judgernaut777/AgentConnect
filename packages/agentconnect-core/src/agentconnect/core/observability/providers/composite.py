"""Fan-out with per-provider failure isolation (production handoff Part II + IV).

The composite is what the emitter actually talks to. It holds the configured
provider list and, for every call, invokes each provider in turn. The contract
that makes it safe to wire into the task lifecycle:

* **A provider failure is isolated.** One provider raising never stops the
  others from seeing the event, and — under the default advisory policy — never
  propagates to the caller. The ledger mutation that triggered the emission is
  therefore never rolled back by an observability outage.
* **Policy is explicit.** `advisory` swallows (and records) provider errors;
  `task_blocking` re-raises the first one *after* every provider has had its
  turn, for a deployment that would rather stop than under-observe.

`startup_fatal` is enforced by the emitter/config at construction time (a
provider whose `health()` fails aborts startup); once running, the composite
only knows advisory vs task_blocking.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Optional

from ..model import (
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
from ..provider import AgentObservabilityProvider

_log = logging.getLogger(__name__)


class FailurePolicy(str, Enum):
    advisory = "advisory"          # log and swallow; the ledger is untouched
    task_blocking = "task_blocking"  # re-raise after fan-out
    startup_fatal = "startup_fatal"  # health failure aborts startup (config-time)


class CompositeObservabilityProvider(AgentObservabilityProvider):
    name = "composite"

    def __init__(
        self,
        providers: list[AgentObservabilityProvider],
        policy: FailurePolicy = FailurePolicy.advisory,
    ) -> None:
        self.providers = list(providers)
        self.policy = policy
        #: Every isolated failure, for `observability health` and tests. Bounded
        #: so a persistently-broken provider cannot grow this without limit.
        self.failures: list[dict] = []

    # ------------------------------------------------------------ internals
    def _isolate(self, provider: AgentObservabilityProvider, op: str,
                 fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — isolation is the whole point
            _log.warning("observability provider %r failed on %s: %s",
                         provider.name, op, exc)
            self._record_failure(provider.name, op, exc)
            if self.policy is FailurePolicy.task_blocking:
                raise

    def _record_failure(self, provider: str, op: str, exc: Exception) -> None:
        self.failures.append({"provider": provider, "op": op, "error": str(exc)})
        if len(self.failures) > 256:
            self.failures = self.failures[-256:]

    # ---------------------------------------------------------------- health
    def health(self) -> ProviderHealth:
        rows = []
        for p in self.providers:
            try:
                rows.append(p.health().model_dump(mode="json"))
            except Exception as exc:  # noqa: BLE001
                rows.append({"provider": p.name, "available": False, "detail": str(exc)})
        available = all(r.get("available", False) for r in rows) if rows else True
        return ProviderHealth(
            provider=self.name, available=available,
            detail=f"{len(self.providers)} provider(s)",
            metadata={"providers": rows, "failures": len(self.failures),
                      "policy": self.policy.value},
        )

    # ---------------------------------------------------------------- begin
    def create_session(self, request: SessionObservationRequest) -> list[ObservationHandle]:
        handles: list[ObservationHandle] = []
        for p in self.providers:
            captured: dict = {}

            def _do(p=p, captured=captured) -> None:
                captured["h"] = p.create_session(request)

            self._isolate(p, "create_session", _do)
            if "h" in captured:
                handles.append(captured["h"])
        return handles

    def spawn_process(self, request: SpawnObservationRequest) -> list[ObservationHandle]:
        handles: list[ObservationHandle] = []
        for p in self.providers:
            captured: dict = {}

            def _do(p=p, captured=captured) -> None:
                captured["h"] = p.spawn_process(request)

            self._isolate(p, "spawn_process", _do)
            if "h" in captured:
                handles.append(captured["h"])
        return handles

    # ---------------------------------------------------------------- report
    def append_event(self, event: AgentObservationEvent) -> None:
        for p in self.providers:
            self._isolate(p, "append_event", lambda p=p: p.append_event(event))

    def update_state(self, request: StateObservationRequest) -> None:
        for p in self.providers:
            self._isolate(p, "update_state", lambda p=p: p.update_state(request))

    # ---------------------------------------------------------------- close
    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        for p in self.providers:
            if p.name != handle.provider and handle.provider:
                continue
            self._isolate(p, "close", lambda p=p: p.close(handle, outcome))

    # ------------------------------------------------------ targeted routing
    def provider_named(self, name: str) -> Optional[AgentObservabilityProvider]:
        for p in self.providers:
            if p.name == name:
                return p
        return None

    def attach_info(self, handle: ObservationHandle) -> AttachInformation:
        provider = self.provider_named(handle.provider)
        if provider is None:
            return AttachInformation(
                provider=handle.provider, available=False,
                detail=f"provider {handle.provider!r} is not configured",
            )
        return provider.attach_info(handle)

    def capture_output(self, handle: ObservationHandle, max_lines: int = 200) -> CapturedOutput:
        provider = self.provider_named(handle.provider)
        if provider is None:
            return CapturedOutput(
                provider=handle.provider, handle_id=handle.handle_id,
                detail=f"provider {handle.provider!r} is not configured",
            )
        return provider.capture_output(handle, max_lines=max_lines)
