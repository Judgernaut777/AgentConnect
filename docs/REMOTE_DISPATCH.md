# Router-driven remote-worker dispatch

Run an agentic task on a *different machine* than the one the router runs on — by
shipping the whole task to a trusted remote worker over mutual TLS, instead of
running the agent loop inside the router process. The router decides *where* the
loop runs; everything downstream — result folding, evaluation, state machine — is
identical to the in-process path, so to the caller (Claude via MCP) it is an
ordinary `TaskSummary` with no signal that it ran off-box.

This is **push** dispatch (router-initiated, decision-driven), alongside the
[pull work-queue](WORK_QUEUE.md) (worker-initiated, capacity-driven). Push hands a
whole task to a chosen worker synchronously; pull lets idle workers claim tickets.
Both reuse the same fail-closed trust predicate.

## How it works — a task's journey

Take a task submitted with `execution="agentic"` and privacy class `repo_sensitive`:

1. **Classify.** The router classifies the privacy class and blocks
   `secret_sensitive` outright (unchanged).
2. **Try to place it remotely** — right after classification, *before* any provider
   routing. The router walks the registry and, for each worker, checks two gates:
   - **Trust:** `WorkQueue.may_claim(worker.tier, privacy_class)` — is this worker's
     attested tier allowed to handle this class? For `repo_sensitive`, only
     `local_only` workers pass.
   - **Availability:** `GET /can_accept` — does the worker have capacity right now?

   The first worker that clears both wins. Unreachable or busy workers are skipped.
3. **Dispatch.** The whole `TaskSubmission` is `POST`ed to the worker's `/run` over
   mutual TLS (`HttpAgentRuntime`, a drop-in `AgentRuntime`). The worker runs its
   *own* agent loop with its *own* model and returns a `WorkerResult`.
4. **Fold the result.** The router records it exactly as `_run_agentic` does —
   output artifact to shared memory, evaluation logged, state machine walked
   `RUNNING → ARTIFACTS_WRITTEN → … → COMPLETE`.
5. **Fallback.** If no worker is eligible or available, the task runs **in-process**
   on the local model — the behavior that existed before.

The wire carries only `task_id` + `TaskSubmission` — **never** `RuntimeConfig`, so a
remote worker cannot be made to relax its own gates. It enforces its local policy.

## Why the model matters (the trust boundary)

A remote worker runs its **own** model. In an agentic loop the tool observations
(file contents, shell output, command results) feed *back* into that model — so
dispatching an agentic task means trusting that worker's model with those
observations. This is the same reason in-process agentic is local-only /
trusted-rented.

That is why the trust gate reuses `WorkQueue.may_claim` verbatim — the pull
federation's live, fail-closed predicate: a worker of tier `T` may handle class `P`
only if `routing.yaml`'s `privacy.classes[P]` admits `T`. It is recomputed per task
and denies on any doubt. Registering a worker can only *grant* what routing policy
already allows for its tier; it can never widen it.

## Components

| Concern | Where |
|---|---|
| Dispatch decision + result folding | `router/service.py` — `_select_remote_worker`, `_run_agentic_remote`, and the fork in `submit_task` |
| Trust predicate (reused) | `common/workqueue.py` — `WorkQueue.may_claim` / `allowed_tiers` |
| The wire (pre-existing, drop-in) | `runtime/transport.py` — `HttpAgentRuntime` → `POST /run`, `create_worker_app` |
| Worker registry | `common/config.py` — `RemoteWorkerConfig`, `load_remote_workers()`; `config/remote_workers.yaml` |
| Self-reported usage | `common/schemas.py` — `WorkerResult.usage`; stamped in `runtime/agent.py` / `graph.py` / `results.py` |

`HttpAgentRuntime` already satisfies the same `AgentRuntime.run(task) -> WorkerResult`
protocol as the local runtime, so the router side just chooses *which* runtime to
call — the bulk of the new code is the selection policy and the reconcile/eval/state
tail, both mirroring the existing in-process path.

## Configuration

Off by default: `config/remote_workers.yaml` ships empty, so `load_remote_workers()`
returns `[]` and every agentic task runs in-process. To enable, register workers:

```yaml
remote_workers:
  - worker_id: fleet-box-1
    endpoint: "https://box1.internal:8443"
    tier: local_only          # the tier you ATTEST for this box
    tls:
      mode: mutual
      ca_cert:     "${AGENTCONNECT_FLEET_CA}"
      client_cert: "${AGENTCONNECT_FLEET_CLIENT_CERT}"
      client_key:  "${AGENTCONNECT_FLEET_CLIENT_KEY}"
    capabilities: [coding, review]   # optional; reserved for future matching
```

`tier` can only grant what `routing.yaml` already admits for that tier (fail-closed),
never widen it. The `HttpAgentRuntime` client needs the `[remote]` extra (`fastapi`,
`httpx`) — imported lazily, only when a worker is actually selected, so one-shot-only
deployments never pull it in.

The worker side is the existing worker app: run `create_worker_app(runtime)` behind a
uvicorn launcher terminating mutual TLS. See [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

## Metering

A remote worker runs its own model, so the router cannot meter it through a
`ModelSource` the way the in-process path does. The worker **self-reports** token
usage in `WorkerResult.usage` (`input_tokens` / `output_tokens` / `model_id`); the
router records it in the task evaluation under provider label `remote:<worker_id>`.
No router-side spend is charged — it is the worker's own compute. An older worker
that omits `usage` records zeros and logs `usage_unreported`.

## Failure semantics

Two cases, deliberately handled differently:

- **Unavailable** (no eligible worker, all busy, or unreachable) → silent fallback to
  in-process. Best-effort; the task always makes progress.
- **Accepted, then dropped mid-run** → the task is marked **FAILED** — *not* silently
  re-run in-process, because the worker may already have performed side effects
  (filesystem writes), and re-executing risks doing them twice.

## Deliberate residuals

- **Remote `repo_sensitive` requires an attested `local_only` worker.** The pull-tier
  map (`WorkQueue.allowed_tiers`) does **not** apply the `allow_rented` widening the
  in-process path allows, so a `private_rented` remote worker is *not* eligible for
  `repo_sensitive`. Conservative on purpose.
- **No pre-reservation / budget ceiling** on remote compute (it's the worker's own).
  Usage is recorded for observability, not enforced against a router-side budget.
- **First-fit selection** over registry order. Round-robin / load-aware selection and
  capability matching (the `capabilities` field is parsed but unused) are future work.
