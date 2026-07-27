# Ecosystem consistency review — fixes and deferred remainders (2026-07-27)

A cross-path review (local worker / rented node / cloud provider) confirmed a
set of semantic inconsistencies in cancellation, leases/fencing, crash
recovery, audit correlation, privacy enforcement, and cost reconciliation.
All confirmed findings were fixed in this pass by converging each path on the
**safest existing semantic** (never a new third semantic). This note records
the convergence decisions and the items deliberately deferred with a narrow
safe mitigation in place.

## Convergence decisions (shipped)

- **Terminal is final, everywhere.** `CANCELLED`/`COMPLETE`/`FAILED` task rows
  can no longer be overwritten by any slower writer: the router pipeline
  (`RouterService._transition` and every follow-up metadata write), the work
  queue (`WorkQueue._set_task_state`), and Engine A's subtask/run writes
  (`_record_result`, `reconcile_orphans`) all use guarded compare-and-set
  writes. A late completion against a cancelled task/subtask is discarded and
  audited, never applied.
- **Cancellation reaches the queue.** `RouterService.cancel_task` cancels the
  linked work-queue tickets (`WorkQueue.cancel_for_task`, new terminal ticket
  status `cancelled`), and the claim UPDATE atomically refuses tickets whose
  linked task is `CANCELLED`.
- **Cancel-after-terminal is an error on both engines.** Engine A raises
  `Conflict`; the router now returns `{"error": "already_terminal", ...}` (its
  typed-dict error idiom) instead of a success-shaped note.
- **Subtask execution is fenced.** `queued -> running` is a single guarded
  UPDATE (`Storage.update_subtask_if_status`); a concurrent second runner gets
  `Conflict`, mirroring the work queue's `lease_lost`.
- **Node pool acquire is a reservation.** Per-provider lock across the
  check-then-provision span: concurrent acquires can no longer double-provision
  and orphan (never-terminated, never-billed-down) a rented box.
- **Crashed rented nodes are evicted** (best-effort terminate + cost true-up)
  on dispatch failure, in both the one-shot and agentic rented paths, and both
  paths now release in `try/finally` and bill the min rental window at
  spin-up.
- **Cloud gateway is fail-closed on live failures.** The deterministic stub
  now covers only the credential-less/offline case; a live call that raises
  propagates as `GatewayError` (task FAILED, quota reconciled as failure) —
  never a fabricated `completed` record with fabricated cost.
- **Memory recall honors the Linear withhold rule.** A `secret_sensitive`
  task's title/goal is never sent to any memory backend (Cognee/Graphiti are
  real HTTP POSTs); the pack degrades with an explicit warning.
- **Audit:** manager cancellation and every queue-driven task-state change
  (including refused terminal overwrites) now append to the task's log.
- **Cost:** queue workers' self-reported usage/cost (`Usage.cost_usd`) lands
  on evaluation rows; idle-reap / eviction true up rental cost beyond the
  billed min window; `enqueue_task` no longer leaks orphan task/artifact rows
  on dedup-key retries.
- **MCP-only deployments self-heal:** `build_mcp_server` starts the same
  work-queue lease reaper the HTTP pull transport starts
  (`AGENTCONNECT_REAPER_INTERVAL`, default 30s, `0` disables).

## DEFERRED (narrow mitigation shipped; remainder out of scope for this pass)

1. **Engine A period budget (BudgetManager convergence).** Shipped:
   `RoutePolicy.max_total_cost_usd` — a cumulative cap over actual recorded
   run spend (`Storage.total_run_cost_usd`), enforced in the `budget_allowed`
   gate. Deferred: daily/weekly/monthly *windowed* budgets, spend surfacing in
   the approval prompt, and unifying on `common.budget.BudgetManager` (which
   requires Engine A adopting `quota_records`). Until then, deployments running
   paid workers under Engine A should set `max_total_cost_usd`.
2. **Work-queue cost -> quota/budget integration.** Shipped: worker-reported
   tokens/cost recorded on evaluations (observability + provider scorecards).
   Deferred: feeding queue-path spend into `QuotaLedger`/`BudgetManager`
   admission. Rationale: queue workers spend their *own* compute; billing it
   against the router's budget is a policy decision, not a bug fix.
3. **Memory-backend locality attestation.** Shipped: `secret_sensitive` is
   withheld from all memory backends (the Linear rule). Deferred: a
   per-backend locality/trust attestation so `local_only`/`repo_sensitive`
   queries are refused for backends configured with non-local `base_url`s.
   Today those tiers still recall (backends default to localhost).
4. **Background node reaper / vendor reconciliation.** Shipped: opportunistic
   reap + cost true-up on the rented dispatch path and eviction on crash, so a
   live deployment self-heals without an external loop. Deferred: a dedicated
   periodic node-reaper thread (mirroring the queue reaper) and reconciling
   `NodePool` state against the vendor's control-plane listing (catching boxes
   orphaned by a process crash between `provision()` and the pool write).
5. **Task-state mirror healing beyond the queue.** Shipped: `report()`/
   `reject()` retries and `reap_expired` re-drive the linked task's mirrored
   state for terminal tickets (crash-window desync self-heals). Deferred: a
   generic store-wide invariant sweep.
