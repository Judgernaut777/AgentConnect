# Architecture

This document maps the [handoff specification](../README.md) to the code and
explains the boundaries that keep the system deterministic and safe. Section
numbers (§) refer to the original handoff.

## Three packages, one shared core

```
packages/
  agentconnect-core/          ← shared core: policy-agnostic (pydantic+pyyaml)  → agentconnect.common.*
  agentconnect-router/        ← Machine A: Agent Router MCP — control plane (§4.1) → agentconnect.router.*
  agentconnect-model-manager/ ← Machine B: Local Model Manager — appliance (§4.2)  → agentconnect.model_manager.*
```

They share the `agentconnect` **PEP 420 namespace** (no top-level `__init__.py`),
so import paths are unchanged even though the code lives in three distributions.
The **Router is the primary product** and depends only on `agentconnect-core`; the
Model Manager is an optional install (`agentconnect-router[embedded]`). The core
depends on nothing heavy; the MCP SDK and FastAPI are imported lazily inside
`router/mcp_server.py` and `model_manager/app.py`.

### Inter-service transport: mutual TLS, no shared secret (§7)

The Router↔Manager link authenticates with **X.509 client/server certificates**
signed by a private CA — identity is the certificate, so no bearer token or shared
secret crosses the wire. `HttpLocalClient` builds an `ssl.SSLContext` that pins the
manager's server cert to the CA and presents the router's client cert; the manager
launches uvicorn with `ssl_cert_reqs=CERT_REQUIRED + ssl_ca_certs`, so the
handshake fails for any client not signed by the trusted CA. Cert/key material is
referenced by **path** (env-expandable) in `providers.yaml` `tls`, never inlined.
`ClientIdentityMiddleware` adds an optional per-CN/SAN allowlist (defense in depth;
see `model_manager/tls.py` for the uvicorn ASGI-TLS caveat). The `SecretResolver`
now serves **cloud providers only**.

### Rented-GPU node tier (Goal 4)

A rented box runs the **same** `agentconnect-model-manager` and is reached over the
**same mTLS transport** — it is just another node with a `private_rented` privacy
tier, an hourly cost model (`quota.py` `rented_gpu`, budgeted by
`rental.max_daily_usd`), and a lifecycle (`router/provisioning.py`:
`NodeProvisioner` + offline `StubProvisioner`). Two credential planes: inference =
mTLS (no secret); the rental vendor control-plane key = the only secret, in the
secrets manager, used solely to rent/terminate. A `repo_sensitive` task reaches a
rented node only with explicit `allow_rented` **and** a satisfied trust policy;
`secret_sensitive` never. Scoring adds `rental_setup_penalty` (spin-up/min-window)
and `rental_cost_penalty` (hourly vs. budget), so the router rents only when the
work justifies amortizing the window — the model-switch "is the setup worth it?"
logic, one level up.

### Responsibility split (§26 — the most important rule)

| Concern | Owner | Where |
|---|---|---|
| What should happen (decisions) | Agent Router | `router/routing.py`, `router/service.py` |
| Local execution (residency, generation) | Local Model Manager | `model_manager/residency.py` |
| Credentials | Secrets Manager (via gateway) | `common/secrets.py`, `router/gateway.py` |
| State + artifacts | Shared Memory | `common/memory.py` |
| Task work | Agents | worker return contract in `common/schemas.py` |

Guardrails enforced by structure:
- The Model Manager imports **nothing** from `router/` — it cannot become the
  policy engine.
- Only `router/gateway.py` constructs a `SecretResolver`; secrets never reach the
  agent/MCP layer.

## The deterministic routing flow (§10, §11)

`RouterService.submit_task` (`router/service.py`) implements the numbered flow:

1. **Receive + assign id** → `SharedMemory.create_task`
2. **Classify** → resolve a capability profile (`profiles.yaml` `agent_defaults`)
3. **Privacy class** → `privacy.classify` (§13)
4. **Redaction pass** → `privacy.redact`, stored as a `sanitized_payload`
   artifact (§14). `secret_sensitive` → **fail closed** (REJECTED, never routed).
5. **Quality + token estimate** → `tokens.estimate_io_tokens`
6. **Fetch local status** → `LocalClient.status()` (§5)
7. **Eligibility** → `RoutingEngine.eligibility` applies HARD constraints (§12)
8. **Score** → `RoutingEngine.score`, sort descending
9. **Select** → highest score; build an explainable `RoutingDecision`
10. **Reserve** → `QuotaLedger.reserve` (cloud) / admission (local) (§15)
11. **Dispatch** → `ProviderGateway.call` (§7)
12. **Store** full output in shared memory; **return** compact summary + refs
13. **Log** the routing decision (already recorded, queryable)

Every step is a pure function of `(task, config, live status)`. There is no
randomness in the control plane (§10) — only inside model generation.

### Scoring (§12)

```
score = capability_fit + expected_quality + latency_fit + privacy_fit
      + availability + residency_bonus
      − quota_scarcity_penalty − queue_delay_penalty − model_switch_penalty
      − cost_penalty − opportunity_cost
```

Weights live in `config/routing.yaml` → `scoring.weights`. **Hard constraints
always override scoring** — an ineligible provider never receives a score, it
lands in `rejected_options` with a machine-readable reason. This is why a
`repo_sensitive` task can never be scored onto a cloud provider even if the cloud
score would be higher.

The objective (§12) is *lowest opportunity cost that still satisfies quality,
privacy, latency and context* — not "always local" or "always cloud". Example:
a **public** task the resident model can handle stays local, because using a
scarce free-tier quota for it incurs `opportunity_cost` while the resident model
incurs none.

## Privacy classes → provider tiers (§13)

`config/routing.yaml` → `privacy.classes` is the authoritative table:

| Task class | Allowed provider tiers |
|---|---|
| `public` | local, external, external_paid |
| `low_sensitive` | local, external, external_paid *(after redaction)* |
| `repo_sensitive` | local only |
| `secret_sensitive` | **none** — blocked from any LLM |
| `restricted` | local only |

The redaction layer (`common/privacy.py`) detects API keys, JWTs, SSH keys,
DB URLs, bearer tokens, emails, internal hostnames, and local paths. If a hard
secret is present the payload is marked **not cloud-safe** and the router keeps
it local or blocks it (§14: "if redaction is too destructive, route local").

## Model residency (§16) & profiles (§17)

Agents request a **profile** (`resident_ok`, `coding_patch`, …), never a raw
model name. `RoutingEngine.resolve_local_model` resolves the profile against the
Manager's live status:

- `prefer_resident_model` → if the loaded model satisfies the profile, use it
  (no switch penalty).
- A specialist that isn't loaded pays the full `model_switch_penalty` **unless**
  the switch policy allows it: urgent priority, or enough same-model work has
  accumulated (`min_batch_size_for_switch`), and the current queue is empty.

This prevents the model-thrashing anti-pattern in §16.

## Local Model Manager API (§22)

`model_manager/app.py` exposes `/status /models /queue /metrics /can_accept
/generate /load /unload`. `ResidencyManager` (`model_manager/residency.py`) owns
admission control: context-cap checks against `max_model_len`, active-sequence
limits, and switch detection. The backend is pluggable (`ModelBackend`); the
shipped `StubBackend` is deterministic so the whole system runs offline. Swap in
vLLM/llama.cpp/Ollama/SGLang by implementing `ModelBackend`.

Auth is a shared bearer token; the router resolves it from the secrets manager
and passes it via `HttpLocalClient` — it never appears in agent-visible state.

## Shared memory & context virtualization (§8, §9)

`common/memory.py` is a SQLite store for tasks, artifacts, logs, routing
decisions, and quota records. Large outputs are stored here and **never returned
inline** through MCP. The manager reads detail on demand with
`read_artifact_chunk` (paginated via `next_offset`) and `get_log_slice`
(level/query filtered). `config/routing.yaml` → `mcp_output_policy` caps payload
sizes and forbids returning full logs/files/traces.

## Quota (§15)

`common/quota.py` implements estimate → check → reserve → call → reconcile →
release. Reservations are held in-process (fast, deterministic) so concurrent
agents can't oversubscribe a shared free tier; committed usage is persisted to
shared memory and is what daily-limit math reads. Paid providers additionally
enforce `max_daily_spend_usd`.

## Mapping to the phased plan (§25)

| Phase | Status | Notes |
|---|---|---|
| 1 Minimal local router | ✅ | router + task DB + artifact store + local status + local routing |
| 2 Shared memory & virtualization | ✅ | chunked reads, summaries, state machine |
| 3 Model residency | ✅ | inventory, load/unload, switch policy, admission |
| 4 Cloud gateway | ✅ | registry, secrets, quota ledger, privacy gates, redaction |
| 5 Workload-aware routing | ✅ | scarcity/opportunity/queue/switch scoring, budget enforcement |
| 6 Evaluation & learning | ◻ scaffolded | quota/usage records exist; scoring of provider quality/latency TBD |

Cloud generation calls degrade to a deterministic stub when credentials/network
are absent (`gateway._call_cloud`), so the full pipeline is exercisable offline;
supply real keys in `config/secrets.yaml` (or env vars) to make live calls.

## Extending

- **New provider**: add to `config/providers.yaml` + a `secret_ref` mapping in
  `config/secrets.yaml`. No code change for routing.
- **New agent type**: add capabilities in `RouterService._capabilities_for` and a
  default profile in `config/profiles.yaml` → `agent_defaults`.
- **Real inference backend**: implement `ModelBackend` and pass it to
  `ResidencyManager`.
- **Real secrets manager**: the `op://` refs are the integration point; add a
  resolver kind in `common/secrets.py` or run the `op` CLI.
