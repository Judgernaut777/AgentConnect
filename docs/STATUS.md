# Status — stabilization boundary

AgentConnect is past architecture-building. This file records what is true, what is
deliberately not built, and what the tests do and do not prove. It is the document to
read before proposing work.

**Current state**

| | |
|---|---|
| Stabilization checkpoint | **`28048ed`**, tagged `v0.1.0-mvp-control-loop` at `12f2186` |
| Gate | `.venv/bin/python -m pytest -q` — **876 passing, 3 skipped** (the skips need the optional `safety-secrets` extra) |
| Safety | modular engines; baseline on by default, third-party engines opt-in ([SAFETY.md](SAFETY.md)) |
| Execution backend | `DirectExecutionBackend` (in-process, shipped default) |
| Memory backends | none wired by default; adapters exist for BrainConnect, Cognee, Graphiti |
| Temporal | optional; `agentconnect-core` installs and runs with no workflow server |
| Linear | optional; unconfigured means completion simply fires no hook |

**Feature work is frozen**, with one explicitly commissioned exception since the
checkpoint: phase 1 of the local safety scanner. Otherwise accept only bug fixes found by
running the loop, documentation corrections, and the small CLI ergonomics needed to run
it.

## AgentConnect is standalone

Nothing below is required. AgentConnect runs, and its gate is green, with none of it:

* **BrainConnect** (trusted memory) — optional. Without it, AgentConnect uses local task
  state and the default no-op memory adapter.
* **ComputeConnect** — **does not exist.** AgentConnect ships its own execution, routing,
  and model seams. Contract: [COMPUTECONNECT_CONTRACT.md](COMPUTECONNECT_CONTRACT.md).
* **ToolConnect** — **does not exist.** AgentConnect ships a fixed MCP tool set.
  Contract: [TOOLCONNECT_CONTRACT.md](TOOLCONNECT_CONTRACT.md).
* Linear, Temporal, Cognee, Graphiti, a local model manager — all optional, none
  configured by default.

Optional integrations add capability. None is a dependency, and none may become one
without a decision recorded here.

**On the name.** BrainConnect was renamed from *WikiBrain* on GitHub; its code was not.
The package, the `wiki` CLI, `WikiBrainMemoryAdapter`, and `WIKIBRAIN_URL` /
`WIKIBRAIN_WRITE_TOKEN` still say `wikibrain`, and those names are load-bearing. This
documentation says BrainConnect for the product and `wikibrain` for identifiers.

## Memory boundary

Implemented and validated. Every backend below is **optional**: BrainConnect is an
optional trusted-memory ledger integration, and AgentConnect works without it, using local
task state and the default no-op memory adapter.

* When they are configured: AgentConnect controls **access**, BrainConnect controls
  **trust**, Cognee adds breadth, Graphiti adds temporal reasoning. The `ContextBuilder`
  decides what a manager or worker actually sees.
* `trusted` is the authority signal. `status == "promoted"` is **not** authority. A
  missing `trusted` means untrusted — it fails closed. The verdict may only downgrade.
* **Only the trusted authority enforces `trusted_only`.** Retrieval backends may return
  untrusted breadth; AgentConnect labels, ranks, and filters *after* retrieval. Passing
  `trusted_only` to a non-authoritative engine silently erases breadth and produces a
  falsely reassuring empty context. This is a correctness rule, not a style preference.
* Scopes are resolved broadest-first (`global`, `project:`, `repo:`, `task:`, and
  `manager:`/`worker:`/`model:` where a profile declares them). An unresolvable scope is
  dropped and *reported*, never sent empty.

## Proprietary-agent loop

Implemented and validated end to end, by an automated test and by a manual dogfood run
(see `docs/OPERATOR_GUIDE.md`).

`launch` prepares a workspace, instructions, a claim, and a scoped session token.
`shell` runs the agent in a sanitized environment. Durable work enters the ledger, the
audit reads it without writing, and the operator completes.

The five rules the loop depends on are the **operational contract** in
`docs/BACKPLANE.md`. Each names the code that enforces it and the test that keeps it
enforced.

## Known test-fidelity limits

Worth stating plainly, because a green suite invites more confidence than it has earned.

* **No test exercises real BrainConnect over real HTTP.** `tests/test_agent_loop_e2e.py`
  runs a real HTTP server on a real port that serves *canned* responses — it proves the
  adapter's httpx path, not the ledger. `tests/test_wikibrain_integration.py` drives real
  `wiki.api` **in-process** through a transport shim — it proves the semantics, not the
  wire. Nothing has both halves. Closing that needs `wiki serve`, which belongs to
  BrainConnect.
* **Cognee and Graphiti are exercised only through transport doubles.** Field names and
  shapes are asserted; no real service has ever answered.
* **Temporal is tested against the in-process time-skipping test server**, never a
  deployed cluster.
* **The compliance layer is not a sandbox.** It makes AgentConnect the normal path and
  makes bypasses visible. It does not contain a hostile process. An agent that edits its
  own environment, or opens the SQLite file directly, is stopped by nothing here. That is
  the documented scope, not an oversight. In particular the **CLI** still opens
  `AGENTCONNECT_DB_PATH` directly and is guarded only by `AGENTCONNECT_MODE`; the HTTP and
  MCP transports now authenticate, the CLI does not.
* **The default safety engine is pattern-based.** The `baseline` engine catches the
  credential formats and injection phrasings it has rules for. It is a floor, not an
  adversarial defense: an attacker who knows the rules can write around them. Maintained
  engines (detect-secrets, TruffleHog, Gitleaks, Presidio) are opt-in.
* **Three safety adapters are untested against their real libraries.** `presidio`,
  `gliner`, and `prompt_guard` are implemented and covered by fake-backed tests; neither
  Presidio, GLiNER, nor transformers is installed in this gate. `detect_secrets`,
  `gitleaks`, and `trufflehog` *are* exercised against the real library and binaries.
* **There is no PII detection by default.** The baseline abstains deliberately; partial
  PII coverage reads as coverage. Enable Presidio.

## Known deferred work

* **Safety surfaces beyond the first two.** `artifact_ingest` and `context_output` are
  implemented ([SAFETY.md](SAFETY.md)). `subtask_instruction`, `review_input`, and
  `attempt_decision_notes` are named and have no policy.
* **Containment / spotlighting** for `context_output` — deferred, with reasoning in
  SAFETY.md.
* `wiki serve` — BrainConnect's HTTP transport. **Deferred, and not AgentConnect's task.**
  Tracked here only as a known integration gap. Do not build it from this side.
* Container / microVM isolation for `agentconnect shell` (the `--container` seam is
  designed for and deliberately unbuilt).
* `TaskWorkflow`, `ManagerHandoffWorkflow`, `WorkerPipelineWorkflow`.
* Mem0 / Supermemory adapters; soft user-preference memory. Both explicitly excluded.
* Contradiction *detection* between promoted claims.

## Open defects

Found by validating the integrations. Full detail and reproductions in
[INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md).

**Closed:**

* **AC-1 (was high)** — the HTTP adapter is authenticated. Every route but `GET /health`
  requires a session token, `force` is gone from the ordinary completion route, and the
  override is an operator-only endpoint that demands a written reason. A test drives a
  real HTTP server on a real port and replays the original bypass.
* **AC-2 (was medium)** — `service.authorize()` is now called by the HTTP adapter on every
  route and by the MCP server on every tool. The session token is enforcement, not
  metadata.
* **AC-3 (was medium)** — the MCP catalog is generated from `core.tools.MCP_TOOLS`. One
  list, and a test asserts the server, `.mcp.json`, and the action table agree.
* **AC-4 (was medium)** — BrainConnect's `SafetyRefused` surfaces as `MemorySafetyRefused`,
  distinct from an unreachable backend, a server fault, a bad candidate, and an
  authorization failure. Per-item `safety`, and capture's `safety` + `quarantined`, are
  preserved.
* **AC-5 (was low)** — capture without an `origin_actor_id` is refused by name, before the
  call. No default actor is invented; provenance is not guessed.
* **AC-6 (was low)** — the documented API port no longer collides with `WIKIBRAIN_URL`.

**Open:**

* **AC-7 (low)** — BrainConnect still ships no HTTP server, and the adapter still defaults
  to `:8787`. Nothing exercises the memory contract on the wire. Not AgentConnect's task.

## What would reopen work

Only these, and each needs a concrete reproduction:

1. a bug found by running the loop;
2. a field-shape mismatch against real BrainConnect;
3. a trust or scope mismatch;
4. a migration issue.
