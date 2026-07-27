# ADR 0009 — The final-invocation boundary (argument-bound grants, contract 1.1)

Status: accepted (2026-07-27)

## Context

ADR 0008 made the ToolConnect governor a real, consulted chokepoint — but only at
**prepare time**, over a worker's **declared tool set** (`capabilities().tools`, bare
names, no arguments). That ADR's stated boundary: "per-tool-call interception is
architecturally unavailable, and claiming it would be a lie," because a worker's
internal tool loop is not AgentConnect's data path (`workers.py`).

That claim is correct for an **opaque third-party harness** (Claude Code, Codex, ...)
spawned as an external process whose internal reasoning and tool loop AgentConnect
genuinely cannot see. It is **not** correct for AgentConnect's own **in-process
LangGraph act/tool loop** (`agentconnect-runtime`): `runtime/graph.py`'s `run_tool` node
already has the model's fully-resolved, final tool arguments in hand — `path`,
`content`, `command`, `url`, `text` — immediately before `read_file`/`write_file`/
`run_shell`/`fetch_url`/`memory_sink.capture` execute. ADR 0008 treated both worker
classes identically; this ADR narrows that boundary claim for the one class where
AgentConnect genuinely is on the data path, without disturbing anything ADR 0008 says
about opaque harnesses.

Meanwhile ToolConnect shipped contract 1.1: `authorize` may bind the exact final
arguments (a server-computed canonical-JSON SHA-256 hash) and, on allow, issue a
one-use `grant` that a `redeem` call atomically consumes immediately before execution.
This closes the actual gap: a declared-set allow at dispatch time says nothing about
what arguments the model later decides to pass, and a worker that passed the
prepare-time gate could still be told to `write_file` anywhere, `shell` anything, or
`fetch_url` any host — none of that was ever re-checked once the subtask started.

## Decision

### 1. The declared-set gate (site #2, `core/service.py`) stays, unchanged in kind

`_consult_tool_governor` / `_execute` / `authorize_tool_use` are untouched
functionally. They remain the cheap early filter, run before a worker even spawns —
and for opaque harnesses they remain the *only* honest enforcement AgentConnect can
offer, exactly as ADR 0008 describes. One docstring in `service.py` was edited to
point at this ADR rather than claim declared-set authorization is "the honest scope
of enforcement" full stop — it is the honest scope for that worker class.

### 2. The in-process runtime loop gains a real final-invocation gate (site #4)

`runtime/graph.py::build_execution_graph` gains three keyword-only, all-optional
parameters: `tool_governor`, `governed_principal`, `governed_source_id`. A new
`governed(state, tool_name, final_args, execute)` closure wraps every
side-effecting tool branch in `run_tool` (`read_file`, `write_file`, `list_dir`,
`shell`, `run_tests`, `fetch_url`, `remember`):

1. No governor bound (`tool_governor is None`) → `execute()` directly. Byte-identical
   to today; every existing runtime/browser/memory/delegation test passes unchanged.
2. A bound governor → `authorize(principal, source_id, tool_name, context,
   args=final_args)`. A raised exception, or `not decision.allowed`, refuses:
   execution never happens, and the model sees an `ERROR:` observation naming the
   reason — the same soft-stop idiom the static `allow_shell`/`allow_browser`/etc.
   gates already use.
3. An allow with **no grant** (`decision.grant is None`) also refuses. This is the
   "mixed-fleet" case ported from the client SDK: a governor/server that allowed but
   did not issue a bindable grant offers no better assurance than the old
   declared-set check, and silently falling back to that would recreate exactly the
   gap this ADR closes. There is deliberately **no decision-only downgrade path.**
4. `redeem(grant.grant_id, principal, final_args)` — the SAME mapping that was just
   hashed and authorized, re-passed verbatim, never regenerated. A raised exception,
   or `not redemption.redeemed`, refuses; execution never happens.
5. Only after a successful redemption does `execute()` run. On completion, `record()`
   is called best-effort (`executed` or `error`, carrying the `grant_id`) — recording
   never gates, and a raising audit sink never crashes an already-completed run.

`final_args` is bound per tool: `{"path": ...}` for `read_file`/`list_dir`,
`{"path", "content"}` for `write_file`, `{"command": ...}` for `shell` (the model's
command) and for `run_tests` (the **operator's** `config.test_command` — the model's
`run_tests` args are already ignored for execution, and the governed args match that:
never a template, always the literal value about to run), `{"url": ...}` for
`fetch_url`, `{"text": ...}` for `remember`.

**Local static gates run first, unchanged.** `allow_shell`/`allow_tests`/
`allow_browser`/`allow_memory`/`allow_delegation` are cheap, no-network checks; they
still short-circuit before the governor is ever consulted. The governor only sees a
call the operator's own config already permits in principle.

**`delegate` is deliberately not gated here.** It executes nothing external — it only
records a sub-task for the router to run as a child. That child subtask goes through
the router's normal dispatch, which hits the declared-set gate (site #2) like any
other subtask. Gating `delegate` itself would be enforcing a no-op.

### 3. `ToolGovernor.redeem` is REQUIRED, not an optional subprotocol

The Protocol (`toolconnect_client.py`) now declares `redeem` as a required method,
alongside `authorize`/`record`/`health`/`mode`. A hardened design would let an old
governor lacking `redeem` fall back silently to decision-only enforcement; that
recreates the exact gap this ADR closes, so it was rejected. At runtime, a bound
governor without a usable grant/redeem path (`decision.grant is None` or
`redeem` not callable) refuses execution — an `ERROR` observation, never a silent
downgrade. Only two implementers exist in this codebase — `ToolConnectGovernor` and
the test-only `FakeGovernor` — both updated in this same change.

### 4. Wiring (all additive, default `None`)

- `LangGraphAgentRuntime.__init__` gains the same three keyword params, stored as
  seams (like `_fetcher`/`_memory_sink`, per the existing "seams are wiring, so they
  live here rather than in the frozen `RuntimeConfig`" convention), threaded into
  `build_execution_graph`.
- `RouterService` gains an optional `tool_governor` (+ `governed_principal`) field,
  wired into `_make_local_runtime`'s **built-in** branch only — an injected
  `local_runtime_factory` (bring-your-own `AgentRuntime`) wires its own governance.
- `agentconnect-worker` (`worker_cli.py`) builds an optional governor from the same
  `AGENTCONNECT_TOOLCONNECT_*` env vars `bootstrap.toolconnect_governor_from_env`
  already reads for the declared-set gate, via a small local helper (kept in the
  runtime package rather than importing the core bootstrap module's private state).

Every one of these is `None` by default; nothing changes for a deployment that does
not configure `AGENTCONNECT_TOOLCONNECT_URL`.

## Consequences

- For AgentConnect's own in-process runtime, authorization now genuinely binds to the
  arguments a tool call is about to execute with, not merely the tool's name at
  dispatch time. A worker that passed the prepare-time declared-set gate can still be
  denied per-call — e.g. a `write_file` to a path a policy forbids, even though
  `write_file` itself was declared and allowed.
- The declared-set gate (site #2) is now honestly described as a cheap early filter
  for this worker class, not the enforcement point — ADR 0008's text is not rewritten
  (its claim about opaque harnesses remains true), but is narrowed by this ADR for the
  in-process loop.
- **Residual risk (structural, not hidden):** ToolConnect is a PDP, never a proxy.
  Nothing stops the runtime from executing with args that differ from what it redeemed
  if the two ever diverge in code — mitigated here by `final_args` being a single
  frozen mapping passed to `authorize`, `redeem`, **and** `execute` without
  reconstruction in between (mirrors the client SDK's `governed_invoke` deep-copy
  discipline). A compromised or buggy runtime could still skip the `governed()` wrapper
  entirely; that is detectable (via ToolConnect's audit trail — `grant_issue` with no
  matching `grant_redeem`/`grant_close`) but not preventable by this architecture.
- **Deferred:** a `tool_call_authorized` runtime `EventType`. The runtime package has
  no observability provider to emit through today; ToolConnect's own
  `grant_issue`/`grant_redeem`/`grant_redeem_denied`/`grant_close` audit chain is the
  authoritative per-call trail in the meantime. Cross-package observability plumbing
  is out of scope here.
- `toolconnect_governor` contract bumped **1.0 → 1.1** in both repos' docs
  (`TOOLCONNECT_CONTRACT.md` here; `AGENTCONNECT_CONTRACT.md`/`SERVICE.md` in
  ToolConnect). `EXPECTED_CONTRACT_MAJOR` in `toolconnect_client.py` stays `"1"` —
  unchanged, which is the proof the bump is additive.

## Verification

- `tests/test_toolconnect_client.py`: `authorize(args=...)` puts `args`/`ttl_seconds`
  in the request body and parses `grant`; an allow with args but no grant is a
  mixed-fleet fail-closed deny; `redeem` maps a success and an `args_mismatch`/
  `not_found` deny as normal (non-exception) returns; unreachable/malformed/
  incompatible-major redeem responses fail closed with `unavailable=True`; `redeemed`
  is `True` only for a literal JSON `true`, never inferred from a truthy value.
- `tests/test_runtime_governor.py` (new): no governor → unchanged behavior; allow +
  redeem executes with the exact final args; an authorize-deny stops execution with no
  side effect; a redeem-deny (or a raising redeem/authorize) means no execution either;
  a governor that allows but issues no grant refuses rather than falling back to
  ungoverned execution; `run_tests` binds the operator's `test_command`, never the
  model's args; the local `allow_shell=False` gate still fires before the governor is
  ever consulted; `delegate` never touches the governor; every side-effecting kind is
  proven to authorize with `args is not None` (the args-stripping regression this ADR
  exists to prevent); a raising `record()` never crashes an already-successful run;
  a successful run calls `record(decision_id, "executed", {"grant_id": ...})`.
- `tests/test_tool_governance_chokepoint.py`: `FakeGovernor` extended with the
  grant/redeem surface; all pre-existing declared-set-gate assertions unchanged.
