"""Provider-neutral agent observability (production handoff Parts II–V).

AgentConnect emits a normalized observation event for every lifecycle change and
fans it out to zero or more configured providers. The seam is
:class:`AgentObservabilityProvider`; the wiring is :class:`ObservabilityEmitter`;
the shipped providers are noop, structured-log (JSONL), composite (fan-out),
tmux (the real live terminal), herdr (feature-flagged), and otlp (export seam).
"""

from __future__ import annotations

from .config import ObservabilityConfig
from .emitter import ObservabilityEmitter
from .model import (
    DEFAULT_STATE_FOR_EVENT,
    AgentObservationEvent,
    AttachInformation,
    CapturedOutput,
    EventType,
    ObservationHandle,
    ObservationOutcome,
    ObservationState,
    ProviderHealth,
    SessionObservationRequest,
    SpawnObservationRequest,
    StateObservationRequest,
)
from .provider import AgentObservabilityProvider
from .providers.composite import CompositeObservabilityProvider, FailurePolicy
from .providers.herdr import HerdrObservabilityProvider
from .providers.noop import NoopObservabilityProvider
from .providers.otlp import (
    OtlpExporterObservabilityProvider,
    event_to_otlp_log_record,
    event_to_otlp_logs_payload,
)
from .providers.structured_log import StructuredLogObservabilityProvider
from .providers.tmux import TmuxObservabilityProvider

__all__ = [
    "AgentObservabilityProvider",
    "AgentObservationEvent",
    "AttachInformation",
    "CapturedOutput",
    "CompositeObservabilityProvider",
    "DEFAULT_STATE_FOR_EVENT",
    "EventType",
    "FailurePolicy",
    "HerdrObservabilityProvider",
    "NoopObservabilityProvider",
    "ObservabilityConfig",
    "ObservabilityEmitter",
    "ObservationHandle",
    "ObservationOutcome",
    "ObservationState",
    "OtlpExporterObservabilityProvider",
    "ProviderHealth",
    "SessionObservationRequest",
    "SpawnObservationRequest",
    "StateObservationRequest",
    "StructuredLogObservabilityProvider",
    "TmuxObservabilityProvider",
    "event_to_otlp_log_record",
    "event_to_otlp_logs_payload",
]
