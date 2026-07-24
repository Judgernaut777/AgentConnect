# Ecosystem findings

Defects found **outside AgentConnect** during the 2026-07-24 ecosystem review, which
walked the four sibling repositories — ToolConnect, ComputeConnect, BrainConnect, and the
Connect umbrella (deploy/manifest/docs) — against AgentConnect at this branch's tip. The
AgentConnect-side corrections from that review landed on this branch as code and doc
fixes; the entries below are the remainder: defects whose fix lives in a sibling repo.
Every entry was **verified against a checkout**, not inferred; file and line citations are
given so each can be re-verified or disputed. As with
[INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md), sibling defects are **reported, not
patched** — each belongs to its own repository, and this register only records them (no
GitHub issues were filed; the `gh` CLI is unavailable in this environment). A second
section records the review's AgentConnect findings that are real but deliberately not
acted on under the [STATUS.md](STATUS.md) feature freeze.

---

## Sibling-repo defects (reported, not patched here)

### EF-1 — ToolConnect: `AGENTCONNECT_CONTRACT.md` status header says nothing is implemented; §6b of the same file says the wiring shipped

**Severity: low. Status: OPEN — ToolConnect's to fix.**

`docs/AGENTCONNECT_CONTRACT.md:3` reads:

> **Status: proposal. Nothing here is implemented, and AgentConnect was not modified.**

Both halves are false, and the file contradicts itself: its own §6b (line 214) is titled
"Reference client & wiring (**shipped**)". AgentConnect *was* modified — the ToolGovernor
seam it proposes exists as
`packages/agentconnect-core/src/agentconnect/core/toolconnect_client.py`
(`ToolConnectGovernor`, whose docstring cites "Connect contract §6b"), is bound from the
environment by `bootstrap.toolconnect_governor_from_env()`, and is consulted fail-closed
at the subtask-dispatch chokepoint (`service.py` `_consult_tool_governor`). The same
file's §6 drift note (line 198) also still says AgentConnect's MCP server "registers 17
tools"; it now registers 18 — `authorize_tool` was added
(`packages/agentconnect-mcp/src/agentconnect/mcp/server.py:312`). The three primitives §6
lists as still missing (`claim_review`, `complete_review`, `get_manager_inbox`) remain
correctly missing.

**Suggested fix, in ToolConnect:** reword the line-3 status header to say the governor
seam is implemented in AgentConnect and wired from the environment; in §6, change "17
tools" to "18 tools", noting `authorize_tool` as the addition. Leave the still-missing
primitives list alone — it is accurate.

---

### EF-2 — ComputeConnect: `AGENTCONNECT_INTEGRATION.md` still frames the AgentConnect wiring as to-do, and repeats a stale "unauthenticated on loopback" claim

**Severity: low. Status: OPEN — ComputeConnect's to fix.**

Two staleness problems in `docs/AGENTCONNECT_INTEGRATION.md`, both overtaken by shipped
code on both sides:

1. Lines 64–66 say "only the *wiring from config* is missing", and the "Consumer change
   — for the lead" section (lines 76–86) lists four to-do items for AgentConnect. All
   four are done: `compute_worker_from_env()` exists in
   `packages/agentconnect-core/src/agentconnect/core/bootstrap.py:182` and its result is
   appended to the worker list in `service_from_env`; `config/compute.yaml.example`
   ships; and `tests/test_compute_bootstrap.py` contains the requested
   degrade-to-off-on-malformed test (`test_malformed_yaml_degrades_to_off`), matching
   the memory backend's pattern.
2. The `AGENTCONNECT_COMPUTE_TOKEN` row (line 23) says "ComputeConnect is
   unauthenticated on loopback today; this is forward-compat." ComputeConnect itself now
   ships bearer auth: `_BearerAuthMiddleware` (`src/computeconnect/app.py:86`, wired at
   `app.py:814`) enforces `Authorization: Bearer <token>` on every route but `/health`,
   driven by `TOKEN_ENV = "COMPUTECONNECT_TOKEN"` (`src/computeconnect/config.py:62`).
   One trap worth stating while editing that row: AgentConnect's
   `HttpLocalComputeProvider` sends the token **verbatim** as the `Authorization` header
   (`core/local_compute.py:139-150`; `tests/test_compute_bootstrap.py:42` pins
   `_token == "Bearer sekret"`), so `AGENTCONNECT_COMPUTE_TOKEN` must include the
   `Bearer ` prefix.

**Suggested fix, in ComputeConnect:** reword lines 64–66 and the consumer-change section
to past tense, recording where each item landed; replace the line-23 auth claim with the
`COMPUTECONNECT_TOKEN` reality and the verbatim/`Bearer `-prefix note. Doc-only; no code
change on either side.

---

### EF-3 — BrainConnect: `serve` does not drain the request body on pre-parse refusals, corrupting HTTP/1.1 keep-alive connections

**Severity: low. Status: OPEN — BrainConnect's to fix.** Reproduced on the wire.

`cli/brainconnect/server.py` sets `protocol_version = "HTTP/1.1"` (line 216), so
connections are keep-alive by default. `do_POST` calls `_require_authorized()` (line 361)
*before* `_body()` — deliberately, so an unauthenticated caller's bytes never reach the
JSON parser — but on a 403 the refusal is answered **without reading the body**, and the
unread bytes are then parsed as the next request line on the same connection. Reproduced
live: two pipelined requests on one socket — an unauthenticated `POST /recall` with a
23-byte JSON body (→ 403), then a well-formed `GET /health` — produce

```
"{"query":"x","limit":3}GET /health HTTP/1.1" 400
```

the refused request's body glued onto the next request line. The codebase knows the
hazard: `_method_not_allowed` (lines 301–308) drains up to `MAX_BODY_BYTES` with a
comment saying exactly why — the drain was simply missed on `do_POST`'s pre-body refusal
paths. Three paths are affected: the auth refusal (line 361), the unknown-POST-path
`_not_found` (line 376), and `_body()`'s oversized-body refusal (lines 247–249, which
raises before reading). Harmless for AgentConnect's adapter today — its `httpx` client
opens a fresh connection per call — but protocol-corrupting for any connection-pooling
client.

**Suggested fix, in BrainConnect:** factor `_method_not_allowed`'s drain into a helper
and call it before answering the auth-refusal and `_not_found` refusals; for the
oversized-body refusal, draining is unsafe, so set `self.close_connection = True`
instead.

---

### EF-4 — Connect: `.env.example` and `COMBINED_INSTALL.md` falsely claim `advisory` governor mode means an outage does not block

**Severity: medium. Status: OPEN — Connect's to fix.** The worst of the Connect batch: an
operator relying on it gets a surprise outage-time failure.

`deploy/.env.example:33-36` says "`advisory` = the governor is consulted and logged but
an outage does not block. Default here is advisory so a partial bring-up is not fatal",
and `COMBINED_INSTALL.md:169` says "`advisory` logs but does not block". Neither is true:
AgentConnect's chokepoint (`service.py` `_consult_tool_governor`) never branches on the
governor's mode — mode is emitted only as observation metadata — and
`ToolConnectGovernor.authorize` converts every transport failure, non-200, and garbled
body into a fail-closed deny, which blocks the subtask before the worker spawns. Any
deny **or outage** blocks, regardless of mode. Connect's own `docker-compose.yml:127`
states this correctly ("fail-closed in code: any deny OR outage blocks the subtask,
regardless of this mode value (the advisory/required label is currently informational
only)"), and `COMBINED_INSTALL.md` even contradicts itself six lines later ("It is
**fail-closed** — an unreachable decision point denies"). An operator who trusts the
`.env.example` comment and brings the stack up without ToolConnect will watch every
tool-declaring subtask fail.

**Suggested fix, in Connect:** align both passages with the compose file's wording, and
drop (or invert) the "partial bring-up is not fatal" rationale — ToolConnect must be up
for tool-declaring subtasks to run. Implementing real advisory semantics would be
AgentConnect feature work, explicitly deferred by its ADRs 0007/0008.

---

### EF-5 — Connect: compose/CI require sibling checkouts named `mcp-agentconnect` and `WikiBrain`, but Connect's own quickstarts clone `AgentConnect` and `BrainConnect`

**Severity: medium. Status: OPEN — Connect's to fix.** Breaks the documented quickstart
path at `docker compose build`.

`deploy/docker-compose.yml:22` and `:110` set build contexts `../../WikiBrain` and
`../../mcp-agentconnect`; `manifest/ecosystem.yaml:12`/`:32` record the same
`local_dir` names; `deploy/README.md:27-29` and `:45` hardcode them; the CI workflows
(`ecosystem-ci.yml`, `publish-images.yml`) check out to those paths explicitly and are
self-consistent. But Connect's user-facing quickstarts — `GETTING_STARTED.md:20`/`:68`
and `COMBINED_INSTALL.md:26`/`:40` — instruct `git clone
https://github.com/Judgernaut777/AgentConnect` and `…/BrainConnect`, the repos' actual
published names, producing directories named `AgentConnect` and `BrainConnect`. A user
who follows the quickstart and then runs `docker compose build` per `deploy/README.md`
fails on missing build contexts. (ComputeConnect and ToolConnect match their default
directory names and are unaffected.)

**Suggested fix, in Connect:** add explicit clone-with-target-dir commands to
`deploy/README.md`'s prerequisites (and the quickstarts), e.g. `git clone
https://github.com/Judgernaut777/AgentConnect mcp-agentconnect` and `git clone
https://github.com/Judgernaut777/BrainConnect WikiBrain`. Do **not** rename the compose
contexts/manifest instead — that ripples through both CI workflows, the manifest, and
the Dockerfile-relative paths, all currently self-consistent.

---

### EF-6 — Connect: stale "agentconnect-core does not declare httpx" caveat in three documents; the dependency is declared upstream and Connect's own `deploy/README.md` says so

**Severity: low. Status: OPEN — Connect's to fix.**

`README.md:266-270` ("lazily imports **httpx** … but does not declare it as a dependency
… The Compose image installs it explicitly. Reported upstream."),
`COMPATIBILITY.md:221-226` (same block, ending "the fix is to add `httpx` to
`agentconnect-core`'s dependencies"), and `docs/TROUBLESHOOTING.md:8-14` (entry 1) all
describe the gap as open. It is closed: AgentConnect's
`packages/agentconnect-core/pyproject.toml` declares `httpx>=0.27` (line 23), with a
comment explaining the lazy-import HTTP clients. Connect's own `deploy/README.md:149-153`
already records this ("Deploy-layer workarounds: **None** … that dependency is now
declared (`httpx>=0.27`) upstream"), so Connect's four documents currently disagree with
each other.

**Suggested fix, in Connect:** update the three stale passages to record the upstream
fix, keeping at most a historical note, matching `deploy/README.md`.

---

### EF-7 — Connect: `agentconnect.Dockerfile` header says "three of the nine AC packages" but installs four; `deploy/README.md`'s layout table omits `-router` and keeps "(+ httpx)"

**Severity: low. Status: OPEN — Connect's to fix.**

`deploy/agentconnect.Dockerfile:4` says "We install three of the nine AC packages" and
the header list names only core/api/cli — but lines 20–23 `COPY` four
(`agentconnect-core`, `agentconnect-router`, `agentconnect-api`, `agentconnect-cli`) and
lines 30–32 install all four. "Nine" is correct — AgentConnect's `packages/` holds
exactly nine. `deploy/README.md:18`'s layout row compounds it: "Installs
`agentconnect-core` + `-api` + `-cli` (+ `httpx`) from the AC repo" — omitting
`-router` and retaining "(+ `httpx`)", which the same README's own "Deploy-layer
workarounds: None" section (lines 149–153) disclaims. One nuance for the edit: the
Dockerfile's stated rationale for the router install ("required at import time by the
api's `/route/decide` route") is itself now stale against this branch — `routes_route.py`
imports `RoutingContext` lazily inside the handler and degrades to a documented 503
without the router package — but the install remains wanted, since a deployed API should
actually route.

**Suggested fix, in Connect:** change line 4 to "four of the nine", add an
`agentconnect-router` bullet to the header list (rationale: needed for `/route/decide`
to answer instead of 503), and fix the README row to "core + `-router` + `-api` +
`-cli`", dropping "(+ `httpx`)".

---

### EF-8 — Connect: `deploy/README.md`'s reproducible-build loop checks out tag `v0.1.0-rc2` in all four sibling repos; no repo carries that tag

**Severity: low. Status: OPEN — Connect's to fix.**

`deploy/README.md:45-47`:

```sh
for r in ../../mcp-agentconnect ../../WikiBrain ../../ComputeConnect ../../ToolConnect; do
  git -C "$r" checkout v0.1.0-rc2      # or the tag you are deploying
done
```

Connect's own `manifest/ecosystem.yaml` — its self-declared source of truth for product
commits and tags — records `v0.1.0` for AgentConnect, ComputeConnect, and ToolConnect,
and `v0.1.2-rc1` for BrainConnect/WikiBrain. `v0.1.0-rc2` appears nowhere else in the
Connect repo; no product carries it, and no single tag is shared by all four, so the
copy-paste loop fails as written.

**Suggested fix, in Connect:** replace the uniform loop with four explicit `git -C
<repo> checkout <tag>` lines using the manifest's real tags (or its pinned commits,
which the manifest itself says to prefer over floating tags), keeping the "or the tag
you are deploying" comment.

---

## Known-open items in AgentConnect, deliberately not done under the freeze

Three findings from the same review are real, are AgentConnect's, and are **not** acted
on: each is feature work, and [STATUS.md](STATUS.md) freezes feature work — the loop
accepts only reproduced bug fixes, documentation corrections, and small CLI ergonomics.
Recorded here so the deliberate inaction is legible.

**Cloud-stub silent degradation** ([REVIEW.md](../REVIEW.md) finding 2). When a cloud
secret fails to resolve or a live call raises, `router/gateway.py` (`_call_cloud`, lines
86–105) falls through to a deterministic `[cloud-stub:…] … (no live call).` result that
is indistinguishable from success — `GatewayResult` carries no stub/degraded field, and
the docstring itself says "A production build would remove the stub fallback and surface
errors." This is deliberate offline-first behavior, pinned by tests
(`tests/test_gateway_cloud.py:112`, `:123` assert the stub prefix on exactly these
failure paths) and already documented in the README's degradation notes — so it is
neither a bug with a reproduction nor a doc error. Surfacing the failure — a `stub:
true` flag on `GatewayResult`/`TaskSummary`, or a strict error-raising mode — is a
schema/behavior change visible to consumers: post-freeze work, tracked by the
still-open REVIEW finding 2.

**Remote-dispatch capability matching** ([REMOTE_DISPATCH.md](REMOTE_DISPATCH.md):124).
Push-dispatch worker selection is first-fit over registry order, filtered only by
attested tier and capacity (`service.py` `_select_remote_worker`);
`RemoteWorkerConfig.capabilities` is parsed but unused — and every touchpoint says so
(`common/config.py:148` "reserved for future capability matching (unused today)",
`config/remote_workers.yaml:38`, and the doc itself). Docs and code agree at every point;
the only hazard is mistaking this for the *shipped* pull-queue capability filter
(`WorkQueue.claim_next`'s `required_capabilities` subset test — see
[WORK_QUEUE.md](WORK_QUEUE.md)), which is a different mechanism and does work.
Implementing push-side matching is feature work; the smallest future change is to read
`w.capabilities` in `_select_remote_worker` as a subset filter mirroring the pull path.

**Multi-process quota reservations** ([REVIEW.md](../REVIEW.md) finding 5).
`QuotaLedger` holds live reservations in process memory only — a `dict` behind a
`threading.Lock` (`common/quota.py:76-77`); only *committed* usage is persisted, as the
module docstring states openly. Since `SharedMemory` is file-backed SQLite that multiple
processes can open, two router processes against one database can each pass
`can_reserve` before either reconciles, oversubscribing a shared quota. (The schema is
half-ready: `quota_usage_since` already defensively excludes `status='reserved'` rows
that nothing yet writes.) The fix — persisted, atomically-claimed reservations —
requires a schema plus a cross-process claim protocol: a concurrency design change, not
a bug fix. The smallest future change is exactly that persisted-reservation row, claimed
atomically at reserve time and deleted at reconcile.
