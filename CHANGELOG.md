# Changelog

## Unreleased — 2026-07-24 — ecosystem review: review-finding fixes and doc reconciliation

A sweep over the Connect ecosystem's loose ends: the two remaining code defects
from `REVIEW.md` are fixed, several small hardening gaps are closed, and the
documentation is reconciled with what the code (and the sibling repos) now do.
Defects found in the sibling repositories during the review are registered in
[docs/ECOSYSTEM_FINDINGS.md](docs/ECOSYSTEM_FINDINGS.md).

### Code fixes

* **Rented-node last-used timestamps are real** (`REVIEW.md` finding 4). The
  router service now passes `time.time()` at all four `pool.acquire`/`pool.release`
  sites, so the idle reaper no longer sees warm rented nodes as ancient.
  Regression: `tests/test_nodepool_concurrency.py::test_idle_reaper_spares_recently_used_node`.
* **Artifacts store the full output** (`REVIEW.md` finding 3). The pre-storage
  `hard_max_chars` clamp is removed (`RouterService._clamp` deleted); artifacts
  persist complete and `read_artifact_chunk` pages them back bounded.
  Regression: `tests/test_artifact_full_storage.py`.
* **Governor principal speaks ToolConnect's vocabulary.** `WorkerLocation.cloud`
  now translates to ToolConnect privacy tier `trusted-cloud` (`local`/`rented`
  pass verbatim), per ADR 0008.
* **Config bootstrap degrades instead of crashing.** The compute, toolconnect,
  and memory bootstraps treat a non-mapping YAML block as "off", with a warning.
* **`POST /memory/feedback` resolves its actor via `assert_actor`.** A missing
  `actor_id` defaults to the token's principal; a spoofed one is a 403.
* **`agentconnect-api` no longer hard-imports `agentconnect.router`** at module
  import; `/route/decide` answers 503 when no router is installed.
* **Sibling-checkout discovery.** The contract cross-check and wikibrain
  integration tests honor `WIKIBRAIN_REPO` and auto-find a sibling checkout named
  `BrainConnect` or `WikiBrain`; the hard-coded absolute path is gone.
* **Config corrections.** `quota_scarcity_threshold_pct` moved from `budget:` to
  `scoring:` in both `routing.yaml` copies; `config/workers.yaml.example` is new;
  `toolconnect.yaml.example` no longer promises an advisory-mode ToolsetPack
  fallback (advisory is metadata-only today — every deny blocks).

### Documentation reconciliation

* The registers and contracts now match the shipped code and siblings:
  `brainconnect serve` closed AC-7 upstream (`docs/INTEGRATION_ISSUES.md`), the
  LiteLLM cloud transport replaces the "missing adapter" table
  (`docs/MODEL_ADAPTERS.md`), ToolConnect/ComputeConnect contract status lines,
  tool counts, and line citations are current, and `REVIEW.md` /
  `docs/REVIEW_FINDINGS.md` carry per-finding status against today's code.
* Gate at this commit, with every optional extra installed: **1035 passed,
  11 skipped** (`1070 passed, 7 skipped` with `WIKIBRAIN_REPO` pointing at a real
  BrainConnect checkout). Skip counts are environment-dependent.

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

* ~~**No LICENSE file.**~~ Closed since: the repository ships an Apache-2.0
  `LICENSE` and a `NOTICE`, and all nine packages declare
  `license = "Apache-2.0"` with `license-files = ["LICENSE"]`.
* The packaged default config means a bare `agentconnect-router` runs with zero
  providers until `AGENTCONNECT_CONFIG_DIR` names a real config.
