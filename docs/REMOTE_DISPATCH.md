# Router-driven remote-worker dispatch

Push an agentic task **whole** to a trusted remote worker instead of running the
LangGraph loop inside the router process. The router picks *where* the loop runs;
everything downstream — result folding, evaluation, state machine — is identical
to the in-process path.

This is **push** (router-initiated, decision-driven), distinct from the
[pull work-queue](WORK_QUEUE.md) (worker-initiated, capacity-driven). Both reuse
the same fail-closed trust predicate.

## How it works

When a task is submitted with `execution="agentic"`, right after privacy
classification the router tries to place it on a registered remote worker:

1. **Trust gate.** A worker of attested tier `T` is eligible for a task of privacy
   class `P` iff `WorkQueue.may_claim(T, P)` — the *same* live, fail-closed
   `routing.yaml` `privacy.classes` mapping the pull federation enforces. A remote
   worker runs its **own** model, and in an agentic loop the tool observations
   (file contents, shell output) feed back into that model, so the worker must be
   trusted for the class — exactly why in-process agentic is local-only /
   trusted-rented.
2. **Availability gate.** The first eligible worker that answers `GET /can_accept`
   with `can_accept=true` is chosen. Unreachable or busy workers are skipped.
3. **Dispatch.** The whole `TaskSubmission` is `POST`ed to the worker's `/run` over
   mutual TLS (`HttpAgentRuntime`, which satisfies the same `AgentRuntime.run`
   protocol as the local runtime). The returned `WorkerResult` is folded into
   shared memory and the state machine just like `_run_agentic` does.
4. **Fallback.** If no worker is eligible or available, the task runs **in-process**
   (today's path and guard). Remote is a best-effort optimization.

The wire carries only `task_id` + `TaskSubmission` — **never** `RuntimeConfig`, so a
remote worker cannot be made to relax its own gates. It enforces its local policy.

## Configuration

Register workers in `config/remote_workers.yaml` (ships empty → feature off):

```yaml
remote_workers:
  - worker_id: fleet-box-1
    endpoint: "https://box1.internal:8443"
    tier: local_only          # attested ProviderPrivacyTier
    tls:
      mode: mutual
      ca_cert:     "${AGENTCONNECT_FLEET_CA}"
      client_cert: "${AGENTCONNECT_FLEET_CLIENT_CERT}"
      client_key:  "${AGENTCONNECT_FLEET_CLIENT_KEY}"
    capabilities: [coding, review]   # optional; reserved for future matching
```

`tier` is the tier you **attest** for the box; it can only grant what `routing.yaml`
already admits for that tier (fail-closed), never widen it. The `HttpAgentRuntime`
client needs the `[remote]` extra (`fastapi`, `httpx`) — imported lazily, only when
a worker is actually selected.

The worker side is the existing worker app: run `create_worker_app(runtime)` behind
a uvicorn launcher terminating mutual TLS. See [AGENT_RUNTIME.md](AGENT_RUNTIME.md).

## Metering

A remote worker runs its own model, so the router cannot meter it through a
`ModelSource`. The worker **self-reports** token usage in `WorkerResult.usage`
(`input_tokens` / `output_tokens` / `model_id`); the router records it in the task
evaluation under provider label `remote:<worker_id>`. An older worker that omits
`usage` records zeros and logs `usage_unreported`.

## Failure semantics

- **Availability** fallback happens at *selection* (can_accept / unreachable → skip
  → in-process).
- A `run()` failure **after** acceptance marks the task **FAILED** — it is *not*
  silently re-run in-process, because the worker may already have performed side
  effects (filesystem writes). Fall back on unavailability; fail on mid-flight drop.

## Deliberate residuals

- **Remote `repo_sensitive` requires an attested `local_only` worker.** The pull-tier
  map (`WorkQueue.allowed_tiers`) does **not** apply the `allow_rented` widening the
  in-process path allows, so a `private_rented` remote worker is *not* eligible for
  `repo_sensitive`. Conservative on purpose.
- **No pre-reservation / budget ceiling** on remote compute (it's the worker's own).
  Usage is recorded for observability, not enforced against a router-side budget.
- **First-fit selection** over the registry order. Round-robin / load-aware
  selection and capability matching (the `capabilities` field) are future work.
