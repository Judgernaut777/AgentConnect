# ADR 0005 — Ledger backup/restore and the rc1→current upgrade/rollback path

Status: accepted (2026-07-12)

## Context

The ledger (`~/.agentconnect/agentconnect.db`, SQLite in WAL mode) is the single
source of truth — "if it is not recorded in AgentConnect, it did not happen". A
production deployment needs a tested backup/restore path and a defined
upgrade/rollback story. A naive `cp` of a WAL-mode DB can capture a torn write.

## Decision

### Backup / restore

- `agentconnect backup <dest>` → `SqliteStorage.backup_to`, which uses SQLite's
  **online backup API** (`Connection.backup`) under the connection lock. It copies
  a transactionally consistent snapshot even while the service is mid-write — not a
  file copy.
- `agentconnect restore <src> --yes` → `SqliteStorage.restore_from`, which copies
  the backup *into* the live connection (again via the backup API) under the lock,
  so open handles keep working and readers see the restored contents atomically —
  no file swap, no torn WAL. `--yes` is mandatory because restore overwrites, and
  the CLI refuses `restore`/`backup` from inside a managed-agent session
  (`AGENTCONNECT_MODE` set) — they are operator actions.

Round-trip is proven by `demo_i_backup_restore.py` and
`test_backup_and_restore_round_trip`.

### Upgrade (rc1 → current)

Schema evolution is **additive only**: new columns arrive via `ALTER TABLE ADD
COLUMN` in `SqliteStorage._migrate`, new tables via `CREATE TABLE IF NOT EXISTS`,
both run under the init lock on every `open`. A ledger created by v0.1.0-rc1 (no
delegation columns, no `observation_handles` table) upgrades forward the first
time current code opens it, and every rc1-era row survives untouched.

`demo_h_rc1_upgrade_migration.py` proves this with a genuine cross-version run: it
creates the ledger with **rc1's own `storage.py`** in a real `git worktree` at the
`v0.1.0-rc1` tag, then opens it with current code and asserts the migration + row
survival + that the new reconcile/metrics surface works on the upgraded DB.
`test_rc1_schema_upgrades_and_keeps_rows` covers the same in-process.

### Downgrade / rollback

Two supported procedures, documented in `docs/OPERATIONS.md`:

1. **Additive-tolerant downgrade (no data loss).** Because migrations only *add*,
   older code simply ignores the newer columns/tables and keeps reading the rows it
   understands. Roll the code back; the current DB still opens. (Newer rows that
   rely on new columns lose only the new fields, never the row.)
2. **Snapshot rollback (guaranteed).** Take `agentconnect backup` before every
   upgrade; to roll back, install the old wheels and `agentconnect restore` the
   pre-upgrade snapshot. This is the recommended path for a controlled downgrade.

## Consequences

Backups are cheap and safe to automate (cron `agentconnect backup`). The upgrade
is zero-touch (open migrates). Rollback is either free (additive tolerance) or a
one-command restore from the pre-upgrade snapshot.
