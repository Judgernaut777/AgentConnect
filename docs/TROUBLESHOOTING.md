# AgentConnect troubleshooting

Symptom-first guide. Companion to `docs/OPERATIONS.md`. Focuses on the runtime,
reconcile, and tmux/Herdr failure modes.

---

## Sessions / runs stuck "running"

**Symptom:** `agentconnect metrics` shows `sessions.running` or `runs.running`
that never drains; `audit` complains about a session that clearly ended.

**Cause:** the agent process died without a terminal event (`kill -9`, OOM, host
reboot, dropped tmux pane). The ledger cannot see a crash it was never told about.

**Fix:**

```bash
agentconnect sessions reconcile --older-than 3600 --dry-run   # inspect
agentconnect sessions reconcile --older-than 3600             # sweep
```

Reconciled rows become `abandoned` (sessions) / `failed` (runs) with a `reconciled`
metadata marker (`detected_by: liveness|age`). If a row is *not* swept and you
expected it to be: it is either genuinely alive (the tmux pane's process still
runs — check `agentconnect agents output <id>`) or there is no liveness evidence
and you did not pass `--older-than` (a JSONL-only deployment needs the age gate).

## `/ready` returns 503

**Meaning:** a hard dependency is down — almost always the ledger is unreachable
(bad `AGENTCONNECT_DB_PATH`, permissions, disk full, the file was moved). The
process is still *live* (`/health` is 200); the orchestrator should stop routing,
not restart.

**Fix:** inspect `GET /ready` body → `checks.storage.detail`. Common causes:

- Wrong path: `echo $AGENTCONNECT_DB_PATH`; confirm the file exists and is
  writable by the AgentConnect UID.
- Disk full: WAL cannot checkpoint. Free space; the DB reopens.
- Corruption: restore the last snapshot (`agentconnect restore <backup> --yes`).

## tmux: "no live tmux session for … on socket agentconnect-obs"

**When:** `agentconnect agents attach <id>` reports unavailable, or `output` says
"pane gone".

**Causes & fixes:**

- **The pane already closed** (agent finished, or was reconciled/cancelled). Check
  `agentconnect agents list --task <id>` — a `done/failed/cancelled` state is
  expected; there is nothing to attach to.
- **Wrong socket.** The provider uses a *dedicated* socket, not your default tmux.
  Confirm `AGENTCONNECT_OBSERVABILITY_TMUX_SOCKET` matches what the service runs
  with, then `tmux -L agentconnect-obs ls`.
- **tmux server died.** With `remain-on-exit on` a finished command leaves a dead
  (readable) pane and the server persists; if the whole server is gone, all handles
  are stale — run `agentconnect sessions reconcile` to mark them terminal.

## tmux: dead panes accumulating

**Symptom:** `tmux -L agentconnect-obs ls` shows many windows with dead panes.

**Explanation:** intentional. `remain-on-exit on` keeps a finished agent's pane
readable so an operator can see *why* it ended, instead of the window vanishing.
`close()` (on a clean terminal transition) and `reconcile` (on a crash) kill the
pane and reap the empty session. If you want to force-clean everything:

```bash
agentconnect sessions reconcile          # terminal-mark handles for dead panes
tmux -L agentconnect-obs kill-server     # last resort: tears down the dedicated server only
```

Killing the dedicated observability server never touches your own tmux (different
socket).

## tmux: the provider is unhealthy at startup

**Symptom:** `agentconnect observability health` shows tmux `available: false`, or
(under `AGENTCONNECT_OBSERVABILITY_FAILURE_POLICY=startup_fatal`) the service
refuses to start.

**Fix:** `tmux -V` must succeed on the service's PATH. If tmux is absent, either
install it or drop `tmux` from `AGENTCONNECT_OBSERVABILITY`. Under the default
`advisory` policy a broken tmux is dropped-with-warning and the JSONL provider still
records everything; only `startup_fatal` makes it abort.

## Herdr: "Herdr provider is disabled" / NotImplementedError

**Expected.** The Herdr provider is feature-flagged off and *refuses to fake* a
connection (ADR 0002). Disabled → it reports why. Enabled without a socket → raises.
Enabled with a socket → the transport is `NotImplementedError` until a real Herdr
control socket exists. Use the tmux provider, which is the real live-terminal
provider on this host. Do not "enable" Herdr expecting it to work — it will not
pretend to.

## Observability events look empty / `agents events` returns nothing

- Confirm a provider is configured: `AGENTCONNECT_OBSERVABILITY` must name at least
  `structured_log` (the always-on JSONL). Unset ⇒ noop ⇒ no events by design.
- Confirm the JSONL path is writable: `AGENTCONNECT_OBSERVABILITY_LOG_PATH`
  (default `~/.agentconnect/observability/events.jsonl`).

## A provider is failing and I see `provider_failures` climbing

`metrics().observability.provider_failures` counts isolated provider errors. Under
the default `advisory` policy these never corrupt the ledger — a provider outage is
swallowed and logged, the task proceeds. Inspect `agentconnect observability
health` (the composite records the last failures). If a provider is chronically
broken, remove it from `AGENTCONNECT_OBSERVABILITY`; the ledger and the remaining
providers are unaffected.

## Restore did not roll back what I expected

`agentconnect restore <src> --yes` overwrites the *entire* ledger with the snapshot
— anything written after the snapshot is gone. If you restored the wrong file,
restore the correct snapshot. Always `agentconnect backup` before a restore if the
current state might still be needed. Restore is refused inside a managed-agent
session (`AGENTCONNECT_MODE` set) — run it as the operator.

## Upgrade opened but data looks wrong

The migration is additive; it never rewrites rows. If counts changed after an
upgrade, you are almost certainly pointed at a different `AGENTCONNECT_DB_PATH`.
Confirm the path, then compare `agentconnect metrics` before/after. To roll back,
reinstall the old wheels and (if needed) `agentconnect restore` the pre-upgrade
snapshot (`docs/OPERATIONS.md` §6).
