# Temporal fork — an optional durable-execution substrate

The zero-infra **SQLite `WorkQueue` is the default** and needs no server. This is an
**optional** alternative backend (`agentconnect-temporal`, behind the `[temporal]`
extra) for deployments that **already run a Temporal server**. It rents Temporal's
commodity mechanics while the differentiated authorization stays ours.

## Why fork instead of replace

The audit put ~1,000 LOC of the WorkQueue in the "commodity mechanics" bucket (atomic
claim, leases, reaper, durable retry, DAG). Temporal provides all of that natively. But
every credible durable-queue buy imposes a **server** — which breaks AgentConnect's
single-box, `pip install`-and-go design center. So the WorkQueue stays the default, and
Temporal is a *swappable* backend, not a rip-and-replace.

## Topology: pull+mTLS vs push+task-queue

| | Default (`WorkQueue`) | Temporal fork |
|---|---|---|
| Model | **pull** — workers `GET /queue/next` | **push** — server assigns activities |
| Identity | mTLS client cert → tier, per claim | which per-tier **task queue** a worker polls |
| Authorization | `WorkQueue.may_claim(tier, class)` | admission **activity** + per-tier queues |
| Retry / reaper / heartbeat | hand-rolled over SQLite | native (retry policy / timeouts / heartbeats) |
| DAG (`depends_on`) | `work_queue_deps` + CTE | child workflows |

They are genuinely different topologies — this is a re-expression, not a drop-in adapter.

## What stays ours (the moat)

- **The privacy×tier rule.** Both substrates call the *same* pure predicate,
  `agentconnect.common.privacy.admits(routing, tier, class)` — the single fail-closed
  source of truth. In the Temporal path it runs as the `agentconnect_admit` activity
  **before** any execution; a denied task never reaches the runtime.
- **Per-tier task queues.** Deployment convention: run one worker pool per trusted tier,
  each polling only its own `task_queue`, so `repo_sensitive` work is only ever pushed to
  `local_only` workers.
- **The `AgentRuntime`.** The `agentconnect_execute` activity runs the *same* runtime
  seam the router uses (built-in `LangGraphAgentRuntime` or a bring-your-own runtime via
  the factory). Temporal re-invokes it on retry; the runtime's own mid-run resumability
  (`checkpoint_root`) makes a re-invocation resume rather than restart.

## What Temporal owns (the rented mechanics)

Durable execution across a worker crash · retry policy (`maximum_attempts` == our
`attempts`/`max_attempts`) · activity heartbeats + timeouts (== lease renew + reaper) ·
child workflows (== the dependency DAG).

## Shape

`agentconnect.temporal.substrate`:

- `AgentTaskParams` — serializable workflow input (task, `privacy_class`, `attested_tier`, …).
- `TemporalSubstrate(routing, runtime_factory)` — binds policy + runtime to two activities:
  `admit` (async, the shared predicate) and `execute` (sync, runs the runtime in the
  worker's thread pool).
- `AgentTaskWorkflow` — `admit → (deny | execute-with-retry)`.
- `build_worker(client, substrate, task_queue=...)` / `start_agent_task(...)` — deployment helpers.

## Running it

```python
from temporalio.client import Client
from agentconnect.common.config import load_routing
from agentconnect.temporal import AgentTaskParams, TemporalSubstrate, build_worker, start_agent_task

client = await Client.connect("your-temporal-host:7233")
substrate = TemporalSubstrate(load_routing(), runtime_factory=lambda: my_agent_runtime)

# One worker pool per trusted tier:
async with build_worker(client, substrate, task_queue="tier-local_only"):
    result = await start_agent_task(
        client,
        AgentTaskParams(task="…", privacy_class="repo_sensitive", attested_tier="local_only"),
        task_queue="tier-local_only", workflow_id="task-123",
    )
```

Tests run against Temporal's in-process time-skipping test server
(`tests/test_temporal_substrate.py`, skipped without the extra) — no standing server
needed to verify admit/deny/retry.

## Guardrail

No default AgentConnect package imports `temporalio`; the fork is loaded only by a
Temporal worker process. The zero-infra deployment is entirely unaffected.
