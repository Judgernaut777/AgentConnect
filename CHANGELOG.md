# Changelog

## 0.1.0 — 2026-07-12 — first coherent Connect-family release

The first release cut of AgentConnect as part of the Connect product family
(AgentConnect, BrainConnect, ComputeConnect, ToolConnect, under the Connect
umbrella). All nine distributions in this workspace ship as **0.1.0**
(`agentconnect-core`, `-router`, and `-model-manager` were previously versioned
0.2.0 internally; the family release re-baselines everything to one number —
nothing was ever published to an index, so nothing can regress).

### Clean install now works from wheels alone

* **Packaged default config.** `pip install agentconnect-router` used to die at
  startup with `FileNotFoundError: config/providers.yaml` — the wheels shipped no
  config and discovery only ever found a source checkout. `agentconnect-core` now
  packages `agentconnect/common/default_config/` (an **empty** provider registry,
  an empty profile registry, and the fail-closed routing policy) and
  `_discover_config_dir()` falls back to it after the env override and checkout
  searches fail. A clean install starts; it does not invent providers.
* Verified end to end in a fresh wheels-only venv: all seven console scripts
  (`agentconnect`, `agentconnect-api`, `agentconnect-mcp`, `agentconnect-router`,
  `agentconnect-model-manager`, `agentconnect-worker`,
  `agentconnect-temporal-worker`), the HTTP adapter's authenticated surface, and a
  full real managed-agent loop (launch → shell with the real `claude` CLI →
  context → attempt → decision → subtask → artifact → review → audit → operator
  completion).

### Ratified ComputeConnect contract amendments

ComputeConnect's `docs/CONTRACT.md` is the naming authority; both amendments are
documented in `docs/COMPUTECONNECT_CONTRACT.md` and pinned by
`tests/test_local_compute_conformance.py` (all six `LocalComputeProvider` HTTP
routes, exercised over the real httpx path against a stub engine).

* **CA-1** — `LocalRunRequest.privacy_tier` (optional) now rides in the
  `POST /generate` body so the engine can re-verify the privacy decision made at
  estimate time. `None`/absent means "assume the most restrictive tier".
  `LocalModelManagerWorkerAdapter` populates it from the subtask's tier.
* **CA-3** (the run_id half of the original CA-2) — `/generate` responses carry `run_id`, surfaced as
  `LocalRunResult.run_id`, making `POST /runs/{run_id}/cancel` usable. Older
  engines that omit it are tolerated. Dispatch-by-reference is deliberately not
  implemented; `/generate` remains a thin streaming proxy.

### BrainConnect rename tolerance

BrainConnect is WikiBrain renamed (module `wiki` → `brainconnect`, service string
`"wikibrain"` → `"brainconnect"`). AgentConnect accepts both spellings everywhere
a backend name is matched: trusted-authority resolution, context-profile backend
selection, bootstrap (`BRAINCONNECT_URL` joins `WIKIBRAIN_URL`), the runtime
memory sink's CLI discovery, and the deny-lists (`brainconnect_promote`/`_admin`
join their `wikibrain_*` counterparts — old spellings stay denied). Aliasing
confers nothing: trust still requires the trusted-authority role and the
authority's own verdict.

### Memory backend authentication

* **Bearer tokens now plumb from env into the memory adapters.** `brainconnect
  serve --token` (and `BRAINCONNECT_TOKEN`) protect the server, but the
  AgentConnect side had no way to supply the token, so every recall/capture
  against a protected server silently degraded to a `MemoryAuthorizationError`
  warning. `memory_from_env` now reads a per-backend token — `WIKIBRAIN_TOKEN`,
  `BRAINCONNECT_TOKEN`, `COGNEE_TOKEN`, `GRAPHITI_TOKEN` (or a per-backend `token:`
  in `memory.yaml`) — into the adapter's `api_key`, sent as the Authorization
  header and never logged. With no token set, `api_key` stays `None` (unchanged).

### `memory promote` can supply confidence and scope

* **`agentconnect memory promote` gained `--confidence` and `--scope`.** The
  service and adapter already forwarded both to BrainConnect's
  `/candidates/{id}/promote`, but the CLI exposed neither, so a typical
  agent-captured candidate always failed `invalid_request` — BrainConnect refuses
  to guess confidence, and refuses to guess scope for a candidate that proposed
  none. `--confidence` is constrained to `low|medium|high|verified`; `--scope`
  takes a descriptor (`global`, `repo:my-app`, `project:x`). Both are optional (a
  backend that can infer them still works) and forwarded verbatim when supplied.

### Standalone posture, re-proven

The backplane imports, serves, and degrades gracefully with **none** of
BrainConnect, ComputeConnect, or ToolConnect present — pinned by a gate test and
verified empirically in the wheels-only venv.

### Known gaps

* **No LICENSE file.** Package metadata says MIT; the repository ships no license
  text. Ecosystem licensing is decided at the Connect level.
* The packaged default config means a bare `agentconnect-router` runs with zero
  providers until `AGENTCONNECT_CONFIG_DIR` names a real config.
