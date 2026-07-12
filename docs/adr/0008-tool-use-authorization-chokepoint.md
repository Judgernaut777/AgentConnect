# ADR 0008 — The tool-use authorization chokepoint (making the governor real)

Status: accepted (2026-07-12)

## Context

ADR 0007 shipped a fail-closed `ToolConnectGovernor` client and the seam to bind it
(`AgentConnectService.tool_governor`, `bind_tool_governor`,
`bootstrap.toolconnect_governor_from_env`). It explicitly deferred *consulting* the
governor from the runtime: "consulting the governor from the worker runtime [is] out
of scope here … this ADR ships the client and the config/bind seam only."

That left a dead seam. The governor could be configured and bound, but **no execution
path ever called `authorize()` on it.** A deployment that configured a ToolConnect
policy engine got no enforcement: every tool the workers declared ran unauthorized.
Worse, the only event that *looked* like tool authorization —
`AgentConnectService.authorize()` emitting `EventType.tool_authorized` for every
task-scoped action — was the generic token/scope gate, not a tool decision at all. An
operator watching for `tool.authorized` saw a stream of them and none of them meant a
tool had been authorized.

Two things had to be true and were not:

1. A real, consulted chokepoint where a bound governor actually decides, fail-closed.
2. A `tool.authorized` event that means *a tool was authorized*, distinct from the
   generic action gate.

## The architectural boundary (what we can honestly enforce)

`workers.py` is explicit that a worker's **internal tool loop is not AgentConnect's
data path**: "its internal tool loop, scratch context, and reasoning are explicitly
*not* truth." A worker is a `{harness, model, tools, sandbox, privacy_tiers}` tuple;
the harness runs whatever tools it runs, and AgentConnect never sits between the
harness and a tool call. The `ToolGovernor` protocol deliberately has **no `invoke`**
(ADR 0007) for the same reason — ToolConnect authorizes and records, it is never a
runtime proxy.

So per-tool-call interception is architecturally unavailable, and claiming it would be
a lie. What *is* honestly enforceable is the **declared tool set**: a worker's
`capabilities().tools`. That set is known before the worker spawns, and refusing a
subtask whose declared tools a policy forbids is real, meaningful enforcement at the
grain the architecture actually exposes.

## Decision

### 1. A genuine consultation method: `_consult_tool_governor`

`AgentConnectService._consult_tool_governor(tools, *, source_id, principal, …)` is the
one place a tool-use authorization is decided:

- **No governor bound → permissive no-op.** Returns `ToolUseAuthorization(allowed=True,
  governed=False)` and emits nothing. Standalone AgentConnect is byte-for-byte
  unchanged; the absence of a policy engine is not a denial of every tool (the
  contract's asymmetry, ADR 0007).
- **A governor bound → fail-closed, per tool.** Each declared tool is consulted. The
  first deny — a *policy* deny, an `unavailable` outage deny (unreachable / non-200 /
  garbled / incompatible contract major, per the client), **or a governor that
  raises** — refuses the whole set. No path turns an outage into an allow.
- Each consultation emits a **`tool.authorized`** observation carrying the ToolConnect
  `decision_id`, `allowed`, `unavailable`, `default_deny`, and determining policies,
  with `outcome=denied` on a deny — so an operator sees tool authorizations distinctly.
- The outcome is recorded best-effort via `governor.record()`: `"authorized"` for a
  granted tool, `"blocked"` for the denied one. Because we authorize the *declared
  set* and never observe the harness's per-call invocation, the honest recorded
  outcome is the grant/block, never a fabricated invocation result. Recording never
  gates a decision (an audit-path outage is not a deny).

A worker declares tools as bare names, so each is attributed to the worker's
**harness** as its `source_id` (`split_tool_ref` also accepts a `"source:name"`
qualifier). The principal is `{id: worker_id, kind: agent, privacy_tier:
worker.location}` — a `local` worker authorizes as a `local` principal, which maps
directly onto ToolConnect's principal model.

### 2. Wired into a real path: worker preparation (`_execute`)

`_execute` — the code that runs immediately before `worker.run` on every subtask —
consults the governor on `caps.tools` **before any worker spawns**. On deny,
`_block_subtask_on_governor` moves the subtask `queued → failed` (no run row, no
worker spawn, no artifact), records a failed attempt so the refusal is durable in the
ledger, and emits `subtask.denied` with the blocking tool and whether the deny was a
policy rule or a fail-closed outage. This is a real path: the default
`DirectExecutionBackend` runs it inline on `submit_subtask`, and the Temporal backend
runs it in the `run_worker` activity.

### 3. An explicit surface: `authorize_tool_use` + the `authorize_tool` MCP tool

`AgentConnectService.authorize_tool_use(token, tools, …)` does the normal token/scope
check via `authorize()` for a new `authorize_tool` action (added to
`MANAGER_ACTIONS`), then consults the governor. The `agentconnect-mcp` server exposes
an `authorize_tool` tool that routes to it, so a manager can ask whether a declared
tool set is permitted before delegating work that needs it. (HTTP/CLI can route to the
same method later; the method is the canonical surface.)

### 4. Event reconciliation

`authorize()` now emits **`action.authorized`** (a new `EventType`) for the generic
token/scope gate. `tool.authorized` is reserved for a governor consultation. The two
no longer conflate: `action.authorized` = "this token may perform this ledger action";
`tool.authorized` = "the governor decided this tool's use." Neither double-counts the
other.

## Consequences

- A configured ToolConnect policy engine now actually constrains AgentConnect:
  forbidden declared tools block their subtask, fail-closed, and every decision is
  observable and audited on both sides.
- Standalone AgentConnect (no governor) is unchanged, and no `tool.authorized` event
  is fabricated when nothing decided one.
- Enforcement grain is the **declared tool set at prepare time**, not per-call
  interception — the honest boundary the harness/data-path split (`workers.py`, ADR
  0007's no-`invoke` protocol) permits. This ADR does not claim otherwise.
- Still out of scope: `advisory`-mode fallback semantics and cached `ToolsetPack`
  resolution (no `/resolve_toolset` route exists yet).

## Verification

- `tests/test_tool_governance_chokepoint.py`: no-governor no-op (and no fabricated
  `tool.authorized`); allowed declared set runs and fires real `decision_id`s and
  records grants; a policy deny blocks the subtask before the worker runs (proven by a
  worker whose `run` raises if reached), durable as a failed attempt; a governor that
  raises fails closed with `unavailable=True`; the action gate emits
  `action.authorized` not `tool.authorized`; `authorize_tool_use` enforces the token
  gate and cross-task scoping.
- `examples/demo_governor_chokepoint.py`: end-to-end against a real `toolconnect
  serve` on a scratch port + scratch DB with a policy that allows one tool and forbids
  another — allowed set proceeds and fires a real ToolConnect `decision_id`, forbidden
  tool blocks the subtask, and killing ToolConnect makes the next authorization
  fail closed.
