# AgentConnect operations runbook

Operator-facing runbook for running AgentConnect in production: health/readiness,
metrics, the orphan-reconcile pass, backup/restore, upgrade/rollback, and the
observability (tmux/Herdr) control surface. Companion: `docs/TROUBLESHOOTING.md`.

Everything here is exercised by `tests/test_reconcile_ops.py` and the demos under
the Wave-A scratch dir (`demo_e`…`demo_j`). ADRs: `docs/adr/0003`–`0005`.

---

## 1. Processes and state

- **The ledger** is a single SQLite DB, default `~/.agentconnect/agentconnect.db`
  (override with `AGENTCONNECT_DB_PATH`), opened in WAL mode. It is the source of
  truth: *if it is not recorded in AgentConnect, it did not happen.*
- **The HTTP adapter** (`agentconnect.api.app:create_app`, `python -m
  agentconnect.api.app`) is stateless over the ledger. Default bind
  `127.0.0.1:8790` (`AGENTCONNECT_API_HOST` / `AGENTCONNECT_API_PORT`).
- **The CLI** (`agentconnect …`) opens the same ledger directly and needs no
  server for operator tasks (metrics, reconcile, backup, restore).

## 2. Health vs readiness (probes)

Two distinct signals — do not conflate them (ADR 0004):

| Probe | Endpoint | Meaning | Failure action |
|-------|----------|---------|----------------|
| Liveness | `GET /health` → 200 | process is up and answering | restart the process |
| Readiness | `GET /ready` → 200 / **503** | can serve traffic (ledger reachable) | stop routing; do **not** kill |

Both are unauthenticated (they expose no ledger data). Example k8s:

```yaml
livenessProbe:  { httpGet: { path: /health, port: 8790 } }
readinessProbe: { httpGet: { path: /ready,  port: 8790 } }
```

Offline equivalent: `agentconnect ready`.

## 3. Metrics

`GET /metrics` (authenticated — pass an operator token) returns JSON:

```json
{
  "tasks":    { "queued": 2, "in_progress": 1, "succeeded": 40, ... },
  "sessions": { "prepared": 0, "running": 1, "ended": 12, "abandoned": 3 },
  "runs":     { "running": 1, "succeeded": 38, "failed": 4 },
  "subtasks": { ... }, "reviews": { ... }, "approvals": { ... },
  "totals":   { "tasks": 43, "sessions": 16, "runs": 43, "artifacts": 88,
                "events": 512, "observation_handles": 16 },
  "observability": { "enabled": true, "provider_failures": 0 }
}
```

Fields cover **sessions, runs, errors** (`runs.failed`, `observability.provider_failures`),
**retries** (a reconciled run carries `metrics.reconciled`), **durations** (each run
row has `started_at`/`finished_at`), and **queues** (`tasks.queued`,
`subtasks.queued`). Offline: `agentconnect metrics`.

Scraping with Prometheus: transcode the JSON in a sidecar; the core deliberately
serves JSON (ADR 0004), not the Prometheus text format.

```bash
TOKEN=$(agentconnect tokens issue --actor operator | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/metrics | jq .
```

## 4. Orphan reconciliation (crash recovery)

A process that dies without a terminal event (`kill -9`, OOM, host reboot, dropped
tmux pane) leaves a session/run stuck `running`. The reconcile pass sweeps such
orphans to a terminal, reconcilable state and revokes their tokens (ADR 0003).

```bash
# Sweep only provider-confirmed-dead processes (safe to run any time):
agentconnect sessions reconcile

# Also time out records with no liveness evidence after 1h (heartbeat timeout):
agentconnect sessions reconcile --older-than 3600

# See what WOULD be reconciled, mutate nothing:
agentconnect sessions reconcile --older-than 3600 --dry-run
```

Report shape: `{reconciled_sessions[], reconciled_runs[], stale_handles[],
checked_sessions, checked_runs}`. Each reconciled record is tagged `reconciled` in
metadata (`reason`, `detected_by: liveness|age`, `prior_status`) so you can always
tell a crash-swept record from a clean finish. The pass is **idempotent** — a
second run finds nothing.

**Recommended cron** (systemd timer or crontab):

```
*/10 * * * *  agentconnect sessions reconcile --older-than 3600 >> /var/log/agentconnect/reconcile.log 2>&1
```

## 5. Backup and restore

The ledger is backed up with SQLite's online backup API — safe while serving, not
a torn `cp` (ADR 0005).

```bash
# Consistent snapshot (safe mid-write):
agentconnect backup /var/backups/agentconnect/$(date +%F-%H%M).db

# Restore (overwrites the live ledger — --yes is mandatory):
agentconnect restore /var/backups/agentconnect/2026-07-12-0900.db --yes
```

**Always `agentconnect backup` immediately before an upgrade** (see §6). Automate
daily backups via cron; keep the last N by timestamped filename.

## 6. Upgrade and rollback

### Upgrade (rc1 → current, and forward)

Schema migration is **additive and automatic**: the first time new code opens an
older ledger it runs `ALTER TABLE ADD COLUMN` / `CREATE TABLE IF NOT EXISTS` under
the init lock. Procedure:

```bash
agentconnect backup /var/backups/agentconnect/pre-upgrade.db   # 1. snapshot
pip install --upgrade agentconnect-core agentconnect-router ... # 2. new wheels
agentconnect ready                                              # 3. opens+migrates; expect ready:true
agentconnect metrics                                            # 4. sanity: counts intact
```

No data is rewritten; every existing row survives (proven: `demo_h`,
`test_rc1_schema_upgrades_and_keeps_rows`).

### Downgrade / rollback

1. **Additive-tolerant (no data loss):** because migrations only *add*, older code
   ignores the newer columns/tables and keeps reading the rows it understands. Just
   reinstall the old wheels; the current DB still opens.
2. **Snapshot rollback (guaranteed):** reinstall the old wheels **and**
   `agentconnect restore /var/backups/agentconnect/pre-upgrade.db --yes`. This is
   the recommended controlled-downgrade path.

## 7. Observability control surface (tmux / Herdr)

The live-terminal provider runs on a **dedicated tmux socket**
(`AGENTCONNECT_OBSERVABILITY_TMUX_SOCKET`, default `agentconnect-obs`) — never the
operator's default tmux server, so reconcile/close can never kill a human's panes.
`AGENTCONNECT_OBSERVABILITY_TMUX_LAYOUT` sets the pane layout hint (default `tiled`).
For export beyond the local JSONL log, `AGENTCONNECT_OTLP_ENDPOINT` names an OTLP
collector base URL — each event is posted to its `/v1/logs` carrying the full
correlation-id set; unset, the OTLP provider does nothing.

```bash
agentconnect observability providers        # configured providers + health
agentconnect observability health           # aggregate
agentconnect agents tree --task <task_id>   # delegation tree
agentconnect agents attach <session_id>     # exact tmux attach command
agentconnect agents output <id> --lines 100 # bounded, redacted scrollback
agentconnect agents cancel <id>             # propagates to the real pane
```

**Socket exposure:** the tmux control socket is a **local Unix socket**
(`$TMUX_TMPDIR` / `/tmp/tmux-<uid>/<socket>`), reachable only by the AgentConnect
UID. Never bind it to a network address. For a remote operator, tunnel over SSH:

```bash
ssh -t operator@host 'tmux -L agentconnect-obs attach -r -t <session>:<window>.<pane>'
```

Herdr's provider is feature-flagged off and refuses to fake a connection (ADR
0002); its remote story is the same — a local control socket reached over SSH,
never a public unauthenticated bind.

## 8. Security posture (operator-relevant)

- Authorization is identical across CLI/MCP/HTTP: every adapter calls
  `AgentConnectService.authorize`. `/metrics` requires a token; `/health` and
  `/ready` are the only unauthenticated routes and expose no ledger data.
- Managed agents (a session with `AGENTCONNECT_MODE` set) cannot complete their own
  task, promote memory, mint operator tokens, or run `backup`/`restore`/`sessions
  reconcile` — those are operator actions the CLI refuses in-session.
- Session tokens are stored only as hashes; a reconcile/`end_shell` revokes them.
- Observation events redact sensitive fields: credential-named keys are masked and
  every string value passes the safety redactor before it is persisted (`demo_j`).
