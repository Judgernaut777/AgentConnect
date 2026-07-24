# ComputeConnect — integration contract

**Status: contract implemented on both sides.** ComputeConnect ships v0.1.0 in the
sibling repository (2026-07-17), and AgentConnect consumes it through the HTTP
local-compute provider; its `docs/CONTRACT.md` is the naming authority for the
shared surface, and two amendments are ratified and implemented on this side (see
§2). Still true and load-bearing: nothing in this repository imports ComputeConnect,
depends on it, or degrades without it. AgentConnect ships every seam below itself,
today, and runs standalone.

This document states what AgentConnect would expect a compute provider to satisfy, so
that the split — if it happens — is a matter of moving implementations behind interfaces
that already exist rather than redesigning AgentConnect. It is written from the code, and
every interface is cited. Where a seam is *not* real, it says so; a contract that
describes a concrete class as an interface is a contract that will be broken on contact.

## The boundary in one sentence

**AgentConnect decides *what* work happens, to which policy, recorded where.
ComputeConnect decides *how* that work is executed and on which model.**

AgentConnect keeps: tasks, subtasks, artifacts, decisions, reviews, audit, the ledger,
privacy classification, budget authority, and the safety pipeline. It does not want to
own: model weights, inference servers, GPU rental, provider SDKs, or the retry semantics
of a cloud API.

## What already exists, and how real it is

Five of these are genuine `abc.ABC` / `typing.Protocol` seams. Three are concrete classes
that other modules import directly. The distinction is the whole cost estimate.

| Seam | Kind | Location | Real? |
|---|---|---|---|
| `ExecutionBackend` | `abc.ABC` | `core/execution.py:67` | **Yes** |
| `LocalComputeProvider` | `abc.ABC` | `core/local_compute.py:101` | **Yes** |
| `WorkerAdapter` | `abc.ABC` | `core/workers.py:90` | **Yes** |
| `LocalClient` | `abc.ABC` | `router/local_client.py:33` | **Yes** |
| `ModelBackend` | `abc.ABC` | `model_manager/backends.py:20` | **Yes** |
| `AgentRuntime` | `Protocol` | `runtime/agent.py:93` | **Yes** |
| `ModelSource` | `Protocol` | `runtime/agent.py:37` | **Yes** |
| `MemorySink` | `Protocol` | `runtime/memory.py:20` | **Yes** |
| `RoutingEngine` | concrete class | `router/routing.py:58` | **No** — imported directly |
| `RouterService` | concrete dataclass | `router/service.py:60` | **No** — but exposes factory callables that are |
| `ProviderGateway` | concrete class | `router/gateway.py:35` | **Partly** — its `completion_fn` is injectable |

A ComputeConnect that implemented only the eight real seams could be adopted without
touching AgentConnect's core. Taking over provider routing additionally requires carving
an interface out of `RoutingEngine` first.

## 1. Execution

`ExecutionBackend` (`core/execution.py:67`) is the seam AgentConnect uses to start and
supervise durable work. It is abstract, it has two implementations already
(`DirectExecutionBackend` in the same file, `TemporalExecutionBackend` in
`agentconnect-temporal`), and the service holds exactly one:

```python
class ExecutionBackend(abc.ABC):
    name: str
    def start_subtask(self, subtask_id: str) -> ExecutionHandle: ...
    def start_review(self, review_id: str) -> ExecutionHandle: ...
    def start_approval(self, approval_id: str) -> ExecutionHandle: ...
    def get_status(self, handle_id: str) -> ExecutionStatus: ...
    def cancel(self, handle_id: str) -> None: ...
    def signal(self, handle_id: str, name: str, payload: dict) -> None: ...
```

The wire types are `ExecutionHandle` and `ExecutionStatus` (`execution.py:42`, `:59`),
with `ExecutionState` ∈ `{running, waiting_approval, waiting_review, completed, failed,
cancelled, unknown}`.

**What AgentConnect requires of any backend.** The canonical control flow is
`DirectExecutionBackend.start_subtask` (`execution.py:116`): prepare worker context, route
the subtask, check approval, run. A backend may make that durable, distributed, or
retried; it may **not** reorder it. Specifically:

* Context preparation goes through `service.prepare_worker_context(subtask_id)`. A backend
  that assembles its own context pack bypasses the `context_output` safety surface and the
  trust labelling. (This was a real bug in the Temporal activity, fixed and pinned by
  `tests/test_backplane_temporal.py`.)
* Approval is checked **before** execution, never after.
* The ledger is AgentConnect's. A backend reports state; it does not write task state.
* `signal` must accept `approval_granted`, `approval_denied`, `cancel_requested`.

**Known gap, stated so ComputeConnect does not inherit it.** The seam is clean but
selection is not wired: `service.py:205` defaults to `DirectExecutionBackend` and
`bootstrap.service_from_env()` never binds Temporal. There is no `AGENTCONNECT_EXECUTION`
environment variable. Outside tests, `bind_execution()` (`service.py:247`) is called by
nobody. A ComputeConnect integration should supply the missing configuration path rather
than assume one exists.

## 2. Local inference

`LocalComputeProvider` (`core/local_compute.py:101`) is already written as a contract for
an *external* engine — it is exactly the ComputeConnect-shaped hole:

```python
class LocalComputeProvider(abc.ABC):
    def inventory(self) -> list[LocalModel]: ...
    def loaded(self) -> list[LocalModel]: ...
    def estimate(self, request: LocalEstimateRequest) -> LocalEstimate: ...
    def run(self, request: LocalRunRequest) -> LocalRunResult: ...
    def health(self) -> dict: ...          # concrete default
```

`HttpLocalComputeProvider` (`local_compute.py:118`) is the reference implementation and
pins the HTTP surface a ComputeConnect service serves:

```
GET  /health          GET  /models        GET  /models/loaded
POST /route/estimate  POST /generate      POST /runs/{id}/cancel
```

ComputeConnect v0.1.0 enforces bearer auth on this surface: when `COMPUTECONNECT_TOKEN`
(or its config `token`) is set, every route but `GET /health` requires it, and a
non-loopback bind refuses to start without one. On this side the credential comes from
`AGENTCONNECT_COMPUTE_TOKEN` (or the compute config's `token:`), sent **verbatim** as
the `Authorization` header and never logged — so the value must include the
`Bearer ` prefix itself. A tokenless loopback deployment remains open by default.

### Ratified amendments (2026-07-12; ComputeConnect `docs/CONTRACT.md` is the naming authority)

Both are additive; both are implemented in this repo and pinned by
`tests/test_local_compute_conformance.py` (the six routes against a stub engine —
a real ComputeConnect is integration-tested against the same shapes separately).

* **CA-1 — `privacy_tier` on `LocalRunRequest`.** AgentConnect owns the change (it
  defines `LocalRunRequest`). The field is optional and rides in the `POST /generate`
  body, so the engine can **positively re-verify** the privacy decision made at
  estimate time instead of trusting the candidate filter alone.
  `LocalModelManagerWorkerAdapter.run` populates it from `subtask.privacy_tier`.
  A `None`/absent tier means the engine must assume the **most restrictive** tier —
  an old caller is never less safe, and this client never invents a looser tier.

* **CA-3 — `run_id` on `/generate` responses.** (ComputeConnect numbered this CA-3;
  it carries the `run_id` half of the original CA-2's motivation.) Responses now carry
  the engine's run identifier, surfaced as `LocalRunResult.run_id`, which makes
  `POST /runs/{run_id}/cancel` actually usable. Engines that omit it are tolerated
  (`run_id = None`). Dispatch-by-reference — CA-2 proper — is **not** implemented;
  `/generate` remains a thin streaming proxy per ComputeConnect invariant 3.

The provider does not participate in routing directly. It is wrapped by
`LocalModelManagerWorkerAdapter` (`local_compute.py:222`) and registered as one worker in
the router's `WorkerRegistry`. **AgentConnect defines the contract and does not own the
engine** — that boundary is deliberate and predates this note.

`estimate()` must be cheap and side-effect-free. It is called during routing, and a
provider that loads a model to answer it will make routing pay for inference.

## 3. Model providers and credentials

This is the part of the contract that matters most, because it is a security boundary.

**Exactly one module in this repository resolves a provider credential**:
`ProviderGateway._call_cloud` (`router/gateway.py:78`), via `SecretResolver.resolve`
(`common/secrets.py:55`). The key is fetched at call time, passed to the transport as an
explicit argument, and never placed in the environment, returned, or logged. Everything
upstream — routing, the runtime, the agent — handles an opaque `secret_ref` string.

**ComputeConnect must preserve this, or the compliance layer is void.** Concretely:

* An agent subprocess must never hold a provider key. AgentConnect enforces this from its
  side with an allowlist, not a denylist: `sanitize_env` (`core/sessions.py`) builds
  the child environment from `BASE_ALLOWLIST` plus `AGENTCONNECT_*` config, so an unknown
  variable is dropped because it was never on the list. `SECRET_DENYLIST`
  (`core/sessions.py`) is defense in depth, not the mechanism.
* A credential must be resolvable from a reference. AgentConnect stores `secret_ref`
  (`common/config.py:107`), never a secret.
* Local inference authenticates with mTLS, not a bearer token: `HttpLocalClient`
  (`router/local_client.py:64`) presents an X.509 client certificate. Identity is the
  cert. A ComputeConnect that swaps this for a shared secret is a downgrade.

`ModelBackend` (`model_manager/backends.py:20`) is the inference-engine seam
(`inventory` / `load` / `unload` / `generate`), selected by `MODEL_MANAGER_BACKEND`. Note
that cloud calls today go through **LiteLLM** (`gateway.py:121`), which is broader
coverage than `docs/MODEL_ADAPTERS.md`'s table implies.

## 4. Routing

Two routers exist, and a contract that conflates them will be wrong about both.

**Core worker routing** (`core/routing.py:219`) is a pure function:
`route(subtask, registry, policy) -> RouteExplanation`. Six hard gates
(`routing.py:24`): healthy, privacy_allowed, capability_match, sandbox_supported,
budget_allowed, approval_granted. Then weighted scoring (`routing.py:34`, summing to 1.0)
with a local-first location term. It is deterministic and explainable — `RouteExplanation`
(`routing.py:68`) carries every rejected worker and every score term, and it is what
`explain_route` returns to an operator.

**Provider routing** (`router/routing.py:58`) is `RoutingEngine`, a concrete class.

**What AgentConnect will not give up.** Privacy classification, the privacy→provider
clamp (`_allowed_privacy_tiers`, `router/routing.py:175`), budget authority, quota, and
the fail-closed `SpendAuthorizer` (`common/authorization.py`, default is
`DenyingSpendAuthorizer`). ComputeConnect may *propose* a placement and must *report* cost
and capability; AgentConnect decides whether the placement is permitted. A compute
provider that could widen its own privacy tier would make the tier meaningless.

The requirement, then: ComputeConnect returns capabilities, availability, and a cost
estimate. AgentConnect applies gates and scoring. **Routing decisions stay explainable and
deterministic** — a scorer that consults an LLM breaks `explain_route`.

## 5. Runtime

`AgentRuntime` (`runtime/agent.py:93`) is a one-method Protocol —
`run(task: TaskSubmission, task_id: str) -> WorkerResult` — and it is already the public
"bring your own runtime" extension point, injected via `RouterService.local_runtime_factory`
(`router/service.py:98`) or served over HTTP by `HttpAgentRuntime` (`transport.py:362`).

`ModelSource` (`runtime/agent.py:37`) is how a runtime reaches a model:
`generate(req: GenerateRequest) -> GenerateResponse`. It exists so the runtime never opens
a provider path of its own and never touches a secret.

One invariant worth writing down because the wire format enforces it: `RuntimeConfig`
(`runtime/agent.py:42`) is **server-side only** and never crosses the remote boundary
(`transport.py:9-13`). The wire carries `{task_id, TaskSubmission}` and returns
`WorkerResult`. Therefore a router — or a future ComputeConnect — **cannot remotely relax
`allow_shell`, `allow_tests`, or `allow_browser`**. Keep it that way.

## What AgentConnect expects, as a checklist

1. Implement `ExecutionBackend`, and supply the configuration path that selects it.
2. Implement `LocalComputeProvider` (or serve its six HTTP routes); keep `estimate()` free.
3. Resolve credentials from a `secret_ref` inside the gateway process only. Never place a
   provider key in any environment an agent can read.
4. Keep mTLS for the local plane.
5. Propose placements; accept AgentConnect's privacy, budget, and approval verdicts.
6. Return explainable, deterministic routing inputs — capabilities, availability, cost.
7. Report state; never write the ledger.
8. Preserve `RuntimeConfig` as server-side-only.

## Non-goals

Not in this contract, deliberately: sandbox or container isolation (AgentConnect is a
compliance layer, not a sandbox); the memory plane (that is BrainConnect's, see
`core/memory.py`); the safety pipeline (AgentConnect owns policy at surfaces it controls,
per [SAFETY.md](SAFETY.md)); and tool discovery/invocation
(see [TOOLCONNECT_CONTRACT.md](TOOLCONNECT_CONTRACT.md)).

## Related

* [ARCHITECTURE.md](ARCHITECTURE.md) — the router / model-manager split as it stands.
* [AGENT_RUNTIME.md](AGENT_RUNTIME.md) — the runtime's own boundary.
* [MODEL_ADAPTERS.md](MODEL_ADAPTERS.md) — provider coverage, honest about gaps.
* [STATUS.md](STATUS.md) — what the test suite does not prove.
