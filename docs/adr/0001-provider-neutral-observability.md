# ADR 0001 — Provider-neutral agent observability

Status: accepted (2026-07-12)

## Context

AgentConnect needs live visibility into the agents it manages — a manager, its
workers, and its reviewers — without binding the ledger to any one terminal
technology. The production handoff (Parts II–V) calls for a provider-neutral
seam so that a JSONL log, an OTLP collector, a tmux server, or a future "Herdr"
live-terminal product are all interchangeable behind one interface, and so that
a standalone install needs no provider at all.

## Decision

1. **One owned seam.** `AgentObservabilityProvider` (in `core/observability`) is
   AgentConnect's, not a vendor's. Every provider implements the same total,
   safe-by-default method set (`health`, `create_session`, `spawn_process`,
   `update_state`, `append_event`, `attach_info`, `capture_output`, `close`).

2. **Lifecycle → normalized event → providers.** The service never touches a
   provider. It calls `ObservabilityEmitter.observe(...)`, which builds one
   `AgentObservationEvent` (the full correlation id set, a normalized state, no
   chain-of-thought) and fans it out through a `CompositeObservabilityProvider`.

3. **Emission can never corrupt the ledger.** Every emission site is guarded by
   `observability.enabled` (a noop deployment does nothing) and, under the
   default `advisory` failure policy, the emitter and composite swallow provider
   errors. Emission always happens *after* the durable ledger write. A
   `task_blocking` policy exists for deployments that would rather stop than
   under-observe; `startup_fatal` aborts startup on an unhealthy provider.

4. **Idempotent, out-of-order tolerant.** Events carry a stable `event_id`
   (dedupe) and a monotonic `sequence` (reorder). A workflow replay or a retried
   activity that re-emits a transition is dropped before fan-out.

5. **The real live provider is tmux.** Herdr is not installable on this host, so
   the concrete production-equivalent is `TmuxObservabilityProvider`:
   session=workspace, window=task, pane=agent — real PTYs, real attach/detach,
   real bounded capture, on a dedicated socket that never touches the user's own
   tmux. `HerdrObservabilityProvider` targets the identical seam and is
   feature-flagged off (see ADR 0002).

6. **Delegation records, not process layout.** `delegation_id` /
   `parent_delegation_id` are persisted on the subtask, review, and session rows,
   so `agents tree` is reconstructed from the ledger without timestamp guessing.

## Consequences

- `pip install agentconnect-core` still runs with zero observability config.
- Adding a provider is one class; the emitter and CLI are untouched.
- The JSONL provider is always-on when configured and is the source for
  `agents events`; OTLP is an additive exporter (ADR references Part V).
