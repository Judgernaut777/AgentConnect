"""OTLP / structured export seam (production handoff Part V).

The rule from Part V, exactly:

* **Local JSONL always.** That is the `StructuredLogObservabilityProvider`; this
  module does not duplicate it.
* **OTLP export when `AGENTCONNECT_OTLP_ENDPOINT` is configured.** Each event is
  mapped to an OTLP LogRecord whose attributes carry the full correlation id set
  (`trace_id`, `task_id`, `delegation_id`, `parent_delegation_id`, `subtask_id`,
  `run_id`, `review_id`) and posted to the collector's `/v1/logs`.
* **No-op when disabled.** Unset endpoint means this provider does nothing and
  touches no socket — the default, and what demo (d) proves.

The mapping is real (see :func:`event_to_otlp_log_record`) and unit-tested; the
network send uses only the standard library (`urllib`), so there is no OTLP-SDK
dependency and the disabled path has zero import cost. `trace_id` is also encoded
into the OTLP top-level `traceId` (hex, 16 bytes) so a collector correlates the
task as one trace without reading attributes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from typing import Optional

from ..model import AgentObservationEvent, ObservationHandle, ObservationOutcome, ProviderHealth
from ..provider import AgentObservabilityProvider

_log = logging.getLogger(__name__)


def _trace_hex(trace_id: str) -> str:
    """A stable 16-byte (32 hex) OTLP trace id derived from the AgentConnect
    trace id, so the same task maps to the same OTLP trace across exports."""
    return hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:32]


def _span_hex(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]


def _attr(key: str, value) -> dict:
    if value is None:
        return {"key": key, "value": {"stringValue": ""}}
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def event_to_otlp_log_record(event: AgentObservationEvent) -> dict:
    """Map one observation event to an OTLP `LogRecord` JSON object.

    Correlation ids become log attributes AND the OTLP `traceId`, so a collector
    can filter by `agentconnect.task_id` or group by trace with equal ease.
    """
    ns = int(event.timestamp * 1e9)
    attributes = [
        _attr("agentconnect.event_type", event.event_type.value),
        _attr("agentconnect.state", event.state.value),
        _attr("agentconnect.outcome", event.outcome.value if event.outcome else ""),
        _attr("agentconnect.trace_id", event.trace_id),
        _attr("agentconnect.task_id", event.task_id),
        _attr("agentconnect.delegation_id", event.delegation_id),
        _attr("agentconnect.parent_delegation_id", event.parent_delegation_id),
        _attr("agentconnect.subtask_id", event.subtask_id),
        _attr("agentconnect.session_id", event.session_id),
        _attr("agentconnect.run_id", event.run_id),
        _attr("agentconnect.review_id", event.review_id),
        _attr("agentconnect.agent_id", event.agent_id),
        _attr("agentconnect.agent_role", event.agent_role),
        _attr("agentconnect.provider", event.provider),
        _attr("agentconnect.workspace_id", event.workspace_id),
        _attr("agentconnect.sequence", event.sequence),
    ]
    return {
        "timeUnixNano": ns,
        "observedTimeUnixNano": ns,
        "severityText": "INFO",
        "body": {"stringValue": event.event_type.value},
        "traceId": _trace_hex(event.trace_id),
        "spanId": _span_hex(event.event_id),
        "attributes": attributes,
    }


def event_to_otlp_logs_payload(event: AgentObservationEvent) -> dict:
    """The full `ExportLogsServiceRequest`-shaped body for one event."""
    return {
        "resourceLogs": [{
            "resource": {"attributes": [_attr("service.name", "agentconnect")]},
            "scopeLogs": [{
                "scope": {"name": "agentconnect.observability"},
                "logRecords": [event_to_otlp_log_record(event)],
            }],
        }],
    }


class OtlpExporterObservabilityProvider(AgentObservabilityProvider):
    """Export events to an OTLP collector when configured, else no-op."""

    name = "otlp"

    def __init__(self, endpoint: Optional[str] = None, timeout: float = 3.0) -> None:
        #: Unset endpoint => hard-disabled. No socket is ever touched.
        self.endpoint = (endpoint or os.environ.get("AGENTCONNECT_OTLP_ENDPOINT") or "").strip()
        self.timeout = timeout
        self.sent = 0
        self.errors = 0

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint)

    def _logs_url(self) -> str:
        base = self.endpoint.rstrip("/")
        return base if base.endswith("/v1/logs") else f"{base}/v1/logs"

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name, available=True,
            detail=f"exporting to {self.endpoint}" if self.enabled else "disabled (no endpoint)",
            metadata={"enabled": self.enabled, "endpoint": self.endpoint,
                      "sent": self.sent, "errors": self.errors},
        )

    def append_event(self, event: AgentObservationEvent) -> None:
        if not self.enabled:
            return  # no-op when disabled — Part V
        payload = json.dumps(event_to_otlp_logs_payload(event)).encode("utf-8")
        req = urllib.request.Request(
            self._logs_url(), data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            self.sent += 1
        except Exception as exc:  # noqa: BLE001 — export is advisory, never fatal
            self.errors += 1
            _log.warning("OTLP export to %s failed: %s", self.endpoint, exc)

    def close(self, handle: ObservationHandle, outcome: ObservationOutcome) -> None:
        return None
