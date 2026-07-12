# ADR 0003 — Orphan reconciliation for crashed sessions and runs

Status: accepted (2026-07-12)

## Context

An agent process can die without emitting a terminal event: a `kill -9`, an OOM,
a host reboot, a dropped SSH pipe to a tmux pane. When that happens the ledger is
left with a `manager_sessions` row stuck in `prepared`/`running`, or a
`worker_runs` row stuck in `running`, forever. That stale row poisons downstream
logic — `audit` measures attempts against a session that ended days ago, a session
token stays live, metrics over-count "running" work — and there is no clean signal
distinguishing "still working" from "crashed and gone".

The pre-existing `abandon_stale_sessions` swept sessions purely by age (24h). That
is too blunt: it cannot reconcile a run at all, it waits a day, and it cannot tell
a genuinely-long session from a dead one.

## Decision

Add a first-class **orphan-reconcile pass**, `AgentConnectService.reconcile_orphans`,
exposed as `agentconnect sessions reconcile [--older-than N] [--dry-run]`.

A record is an *orphan* when it is still in a live status AND either:

1. **Liveness (preferred):** a live-surface provider proves its process/pane is
   dead. Providers implement `is_live(handle) -> True | False | None`. Only a hard
   `False` reconciles from liveness. `None` (the default for providers with no
   terminal, e.g. JSONL/OTLP) is treated as *no evidence* and never triggers a
   sweep on its own — a config change that drops the tmux provider must not
   manufacture dead verdicts. `tmux` implements the probe against `#{pane_dead}`
   and pane existence.
2. **Age gate (fallback):** `--older-than N` elapsed since the record started with
   no liveness evidence — a heartbeat timeout for deployments without a live
   provider.

Orphans are swept to a terminal, *reconcilable* state — sessions to `abandoned`,
runs to `failed` — and tagged `reconciled` in metadata (`{at, reason, detected_by,
prior_status}`) so an operator can always distinguish a crash-swept record from a
clean finish. The pass also revokes the session's tokens, closes its live
observation panes, records a ledger `session_reconciled`/`run_reconciled` event,
and emits the corresponding observation event.

Properties:

- **Idempotent.** A second pass finds nothing, because the first moved every
  orphan to a terminal state (proven by `test_reconcile_is_idempotent`).
- **Never reconciles a live agent.** `is_live -> True` short-circuits even under
  `--older-than 0` (`test_live_session_is_never_reconciled`).
- **Dry-run is side-effect-free** (`test_reconcile_dry_run_mutates_nothing`).
- **Canonical state is never corrupted:** reconcile only moves live→terminal, and
  a run's subtask is failed only if still `running`.

## Consequences

Cron the pass (`agentconnect sessions reconcile --older-than 3600`) as documented
in `docs/OPERATIONS.md`. A crash now heals to a reconcilable record within one
pass instead of wedging the ledger. Stale tmux/Herdr handles are detected in the
same pass (`stale_handles` in the report) and their handle rows marked terminal.

## Evidence

- Tests: `tests/test_reconcile_ops.py` (liveness, age, idempotent, dry-run,
  unknown-is-not-dead, real-tmux end-to-end kill -9).
- Demo: `demo_e_crash_orphan_reconcile.py` (REAL tmux pane, real `kill -9`).
