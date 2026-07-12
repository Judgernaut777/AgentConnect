# ADR 0004 — Liveness/readiness split and a JSON metrics endpoint

Status: accepted (2026-07-12)

## Context

The HTTP surface had a single `GET /health` that reported process state. A
production deployment behind an orchestrator (k8s, systemd, a load balancer) needs
two *distinct* signals:

- **Liveness** — is the process up and able to answer at all? A failed liveness
  probe means "restart me".
- **Readiness** — can this instance serve real traffic right now? A failed
  readiness probe means "stop routing to me, but do not kill me" (e.g. the ledger
  is momentarily unreachable, a migration is running).

Conflating them makes an orchestrator kill a pod it should merely drain, and vice
versa. There was also no metrics surface for sessions/runs/errors/queues.

## Decision

- `GET /health` — **liveness**. Never touches the ledger; returns 200 while the
  process can answer. Public (no token): it reveals no ledger data.
- `GET /ready` — **readiness**. Runs `AgentConnectService.readiness()`, which
  probes the one hard dependency (the ledger) with a live query. Returns 200 when
  ready, **503** when a hard dependency is down. Public: it returns only
  pass/fail of dependency checks, never task data, so a k8s/systemd probe needs no
  credential.
- `GET /metrics` — operational counters as **JSON**:
  `{tasks, sessions, runs, subtasks, reviews, approvals}` status-count maps,
  `totals`, and `observability` gauges (enabled, provider_failures). **Authenticated**
  (`get_status` action) — counts are ledger data, not a public probe.

### Why JSON metrics, not Prometheus text

The rest of the HTTP surface is JSON; an operator curls `/metrics` and reads it
without a scraper; and a Prometheus exporter transcodes JSON→text trivially in a
sidecar. Committing the core to Prometheus's text exposition format would add a
format the codebase uses nowhere else. The endpoint is documented in
`docs/OPERATIONS.md` with the field list and a transcode note.

The same data is available offline via `agentconnect metrics` and `agentconnect
ready` (no server needed).

## Consequences

Probes: `livenessProbe: GET /health`, `readinessProbe: GET /ready`. Scrape
`/metrics` with a token. The readiness check fails closed (`test_readiness_ok_and_degraded`,
`test_http_ready_returns_503_when_storage_is_down`).

## Evidence

- Tests: `tests/test_reconcile_ops.py::{test_metrics_report_status_counts,
  test_readiness_ok_and_degraded, test_http_health_ready_and_metrics,
  test_http_ready_returns_503_when_storage_is_down}`; the authz coverage test now
  asserts only `/health` and `/ready` are unauthenticated
  (`tests/test_http_authorization.py`).
