# ADR 0002 — Herdr observability provider (feature-flagged, blocked on host)

Status: accepted, provider disabled (2026-07-12)

## Context

The production handoff names "Herdr" as the live-terminal observability product
AgentConnect should integrate with. The lead established as fact that **Herdr is
not installable on this host**: there is no binary, no PyPI package, and no
repository to build from. tmux 3.3a *is* installed.

## Decision

- Ship `TmuxObservabilityProvider` as the **real** live-terminal provider and the
  concrete production-equivalent of the Herdr role. It is fully functional today.
- Ship `HerdrObservabilityProvider` coded against Herdr's described control
  surface — the same workspace/tab/pane + attach mapping tmux uses — targeting the
  identical `AgentObservabilityProvider` seam. It is **feature-flagged off** and
  **refuses to pretend**: with `enabled=False` (default) every method is a
  disabled no-op that reports why; constructing it with `enabled=True` while no
  control socket answers raises rather than degrading to a stub. The control-socket
  transport (`_HerdrControlClient.request`) is deliberately `NotImplementedError`.

This is the genuine external blocker: only the Herdr-*specific* provider is
blocked. The provider-neutral architecture and a real live-terminal provider
(tmux) are delivered and demonstrated.

## Exact command to enable once a Herdr control socket exists

```bash
export AGENTCONNECT_OBSERVABILITY=structured_log,herdr
export AGENTCONNECT_OBSERVABILITY_HERDR_ENABLED=1
export AGENTCONNECT_OBSERVABILITY_HERDR_SOCKET=/run/herdr/control.sock
```

and implement `_HerdrControlClient.request()` in
`core/observability/providers/herdr.py` against Herdr's real JSON control
protocol (`workspace.ensure`, `tab.ensure`, `pane.create`, `pane.attach_url`,
`pane.capture`, `pane.kill`, `ping`). Each maps one-to-one onto the tmux
provider's operations, so no other code changes are required.

## Consequences

- No fake Herdr success anywhere in the codebase or tests.
- The seam is proven real by the tmux provider running against the same
  interface, so enabling Herdr later is a transport implementation, not a
  redesign.
