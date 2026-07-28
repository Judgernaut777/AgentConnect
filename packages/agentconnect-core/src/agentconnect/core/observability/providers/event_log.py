"""The always-on durable event-bus provider (docs/EVENT_BUS.md, Path 2).

Registered into every emitter (`AgentConnectService.__init__` / `bind_observability`),
so `ObservabilityEmitter.enabled` is True in every deployment and the rich
`_observe(...)` sites become durable — persisted into the ledger's `event_log`
table via `SqliteStorage.append_bus_event` — with no separate infrastructure to
run or configure. This is the "rich, ordered-after, advisory" producer: it runs
strictly after the ledger commit that triggered the transition (Path 1's
`state.changed` skeleton, written same-commit by `_insert_transition_audit`, is
the guaranteed sibling — see the module docstring in `storage.py`).

`passive = True`: this provider offers no live surface — it never returns a
handle worth attaching to — so the composite skips it in
`create_session`/`spawn_process` (see `providers/composite.py`) rather than
persisting the base class's generic inert placeholder as if it were a real pane.
"""

from __future__ import annotations

from typing import Any

from ..model import AgentObservationEvent, ProviderHealth
from ..provider import AgentObservabilityProvider

#: Keys whose *value* is dropped outright — content, not metadata, could land
#: here if a caller ever passed one by mistake. Defense-in-depth on top of the
#: emitter's own `_redact_metadata` (masks credential-shaped keys, scans every
#: string through the safety redactor before this provider ever sees it).
_DROP_KEYS = frozenset({
    "instructions", "prompt", "transcript", "output", "output_text", "goal",
})

#: Substrings that mark a key as credential-shaped, masked regardless of content
#: (belt-and-suspenders on top of the emitter's own `_SENSITIVE_KEYS` list).
_MASK_SUBSTRINGS = ("token", "secret", "api_key", "apikey", "password",
                    "authorization", "credential")

_MAX_STRING = 500

#: How deep the recursive scrubber will descend into nested objects/arrays
#: before it stops inspecting and withholds the remaining subtree wholesale. A
#: FOREIGN (publish-ingress) payload is attacker-controlled JSON: without a
#: bound, a deeply nested body could exhaust the stack, and — more importantly —
#: a secret buried below the point we inspect would otherwise pass through
#: unscrubbed. The limit is generous for any legitimate event payload.
_MAX_DEPTH = 8


def _scrub_value(value: Any, depth: int) -> Any:
    """Recursively scrub one value. Descends into nested dicts (so credential-
    shaped keys are masked and known-sensitive keys dropped at ANY depth, not
    just the top level) and into lists/tuples, bounds every string, and never
    raises — a value that cannot be represented is replaced with a bounded
    `repr()` rather than dropped silently or leaked whole."""
    if depth > _MAX_DEPTH:
        # Past the bound we refuse to pass the subtree through: a secret nested
        # below here would escape inspection, so fail closed.
        return "[nested too deep]"
    if isinstance(value, dict):
        return _scrub(value, depth)
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    try:
        import json as _json

        _json.dumps(value)
        return value
    except Exception:  # noqa: BLE001 — never let a weird value break scrubbing
        try:
            return repr(value)[:200]
        except Exception:  # noqa: BLE001
            return "[unrepresentable]"


def _scrub(metadata: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    """Drop known-sensitive keys, mask credential-shaped ones, bound every
    remaining string, and recurse into nested objects/arrays so the same key
    masking applies at every depth (a secret nested one level deep must not
    survive — docs/EVENT_BUS.md §9.3). Never raises."""
    if depth > _MAX_DEPTH:
        return {"redacted": "[nested too deep]"}
    out: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        lower = str(key).lower()
        if lower in _DROP_KEYS:
            continue
        if any(s in lower for s in _MASK_SUBSTRINGS):
            out[key] = "[redacted]"
            continue
        out[key] = _scrub_value(value, depth + 1)
    return out


class SqliteEventLogProvider(AgentObservabilityProvider):
    """Persists every rich observation event into the ledger's `event_log`.

    Not a live surface: `health()`/`append_event()` are the only methods this
    provider does real work in; everything else falls back to the inert base
    class defaults (and `passive = True` means the composite never even calls
    `create_session`/`spawn_process` on it)."""

    name = "event_log"
    passive = True

    def __init__(self, storage: Any) -> None:
        self._storage = storage

    def health(self) -> ProviderHealth:
        path = getattr(self._storage, "path", "?")
        return ProviderHealth(provider=self.name, available=True, detail=f"db={path}")

    def append_event(self, event: AgentObservationEvent) -> None:
        # No try/except here: the composite's `_isolate` already fences every
        # provider call (Part IV) — a second layer here would only double-log
        # the same failure. Letting it raise is what makes the failure visible
        # to `composite.failures` / `observability health`.
        payload = _scrub(event.metadata)
        payload.update({
            "agent_id": event.agent_id, "agent_role": event.agent_role,
            "provider": event.provider,
        })
        self._storage.append_bus_event(
            event_id=event.event_id, type=event.event_type.value,
            outcome=event.outcome.value if event.outcome else None,
            actor=event.agent_id, task_id=event.task_id,
            subtask_id=event.subtask_id, run_id=event.run_id,
            review_id=event.review_id, session_id=event.session_id,
            delegation_id=event.delegation_id,
            parent_delegation_id=event.parent_delegation_id,
            workspace_id=event.workspace_id, payload=payload,
        )
