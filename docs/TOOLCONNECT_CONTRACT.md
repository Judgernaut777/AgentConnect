# ToolConnect — integration contract

**Status: design note. ToolConnect does not exist.** No code moves as a result of this
document. AgentConnect exposes a fixed MCP tool set today and runs standalone.

This is an inventory of AgentConnect's tool surface as it actually is, followed by a
proposed division of responsibility. It was first written against a surface with three
code-versus-docs disagreements (AC-1, AC-2, AC-3); those are now closed, and the sections
below describe the reconciled state, keeping the history where it explains a design choice.
An integration contract written against documented rather than real behavior would be
wrong on arrival, so the real behavior is what is written here.

## The boundary in one sentence

**ToolConnect would own the tool *catalog*: what tools exist, how they are discovered, how
a call is authorized. AgentConnect keeps the *consequences*: what a call means for the
ledger, and whether the work it recorded is enough to complete a task.**

## 1. MCP integration points

There are **two** MCP servers in this repository, and they are not interchangeable.

| Server | Package | Entry point | Transport | Role |
|---|---|---|---|---|
| `agentconnect` | `agentconnect-mcp` | `mcp/server.py` (`main`) | stdio (default), sse, streamable-http on `:8765` | The compliance surface a managed agent sees |
| `agentconnect-router` | `agentconnect-router` | `router/mcp_server.py` (`main`) | stdio, or shared on `:8760` | Routing, budget, federated work queue |

Only the first is written into a managed workspace's `.mcp.json` (`core/workspace.py`).
The forbidden-tool guarantee below concerns that server.

`build_mcp_server()` (`mcp/server.py`) registers **17** tools, and its docstring now
points at `core.tools.MCP_TOOLS` as the source of truth rather than naming a count. (It
once claimed "thirteen"; that drift is gone — see AC-3, closed.)

Mutating (write to the ledger): `create_task`, `claim_task`, `release_task`,
`record_decision`, `record_attempt`, `request_review`, `submit_subtask`,
`capture_memory_candidate`, `record_memory_feedback`, and `get_handoff_summary` — which
persists the summary it computes, so it is a write despite its name.

Read-only: `open_task`, `get_status`, `list_artifacts`, `read_artifact_chunk`,
`explain_route`, `recall_memory`, `get_task_context_pack`.

**Every tool body calls a method on `AgentConnectService` and nothing else.** No tool
touches storage, the filesystem, or a backend directly; `mcp/tools.py` is pure response
shaping with no I/O. This is the single most important property to preserve in a split:
the service is the only door.

### The forbidden six

`temporal_signal`, `wikibrain_promote`, `cognee_write`, `graphiti_write`,
`local_model_generate`, `secrets_read` must never be exposed. They are listed in
`DENIED_MCP_TOOLS` (`core/tools.py`), written into `.mcp.json` as `deniedTools` so the
denial is auditable, and independently listed in `AGENT_FORBIDDEN_ACTIONS` /
`NEVER_TOKEN_ACTIONS` (`core/sessions.py`).

Verified: none of the six is registered by either MCP server. The denial is **structural**
— no server offers them — and the denylist is a statement of intent, not the mechanism.
A ToolConnect that introduced dynamic registration would convert this from a structural
guarantee into a policy check, which is a real loss. If it must, the denylist has to
become an enforced filter at registration time, not a hint in a config file.

## 2. Tool discovery

Static registration, single-sourced. There is one catalog — `MCP_TOOLS` in
`core/tools.py` — and everything else is derived from it: the server registers exactly
it, `workspace.EXPOSED_MCP_TOOLS` is generated from it, and `.mcp.json`'s `allowedTools`
comes from that. Adding a tool means editing `core/tools.py`, not `server.py`. There is
no registry table beyond that catalog and no dynamic/plugin discovery.

An agent learns the tool set three ways, all static: the MCP `tools/list` response, the
per-workspace `.mcp.json`, and the injected `AGENTCONNECT.md` / `CLAUDE.md` / `CODEX.md`
instruction files (all written by `core/workspace.py`).

**Why single-sourcing matters — the drift it ended.** `EXPOSED_MCP_TOOLS` used to be a
hand-written list that advertised `get_subtask_status`, a tool no server registered, and
omitted eight that were, every memory tool among them — so a harness honoring
`allowedTools` denied a manager its memory and granted a tool it could not call. That was
AC-3, now **CLOSED**: the catalog is the source of truth and `tests/test_mcp_catalog.py`
asserts the server, `.mcp.json`, and the action table all agree. That two hand-written
lists drifted silently for months is the strongest argument in this document for
ToolConnect owning the catalog — and the reason any dynamic registration it introduces
must enforce the denylist at registration time rather than trust a config file. See
[INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md).

## 3. Permissions

Three mechanisms, of which **only two are wired**.

**Scoped session tokens — enforced.** `mint_token()` issues `act_<urlsafe>`; only its
SHA-256 is stored. `build_scope()` records the mode's action list.
`AgentConnectService.authorize(token, action, task_id=…, review_id=…)` checks expiry,
revocation, the two denial sets, the mode's actions, and the token's task binding. It
raises `Unauthenticated` when the credential cannot be established and `PolicyViolation`
when it can and the answer is still no.

**Every transport calls it.** The HTTP adapter via an app-level `enforce` dependency, on
every route; the MCP server via a decorator around every tool registration, using the
action declared in `core.tools.MCP_TOOLS`. There is one authorization rule and no
transport reimplements it — a rule fixed in `authorize()` is fixed everywhere.

The **CLI still does not authenticate**: it opens `AGENTCONNECT_DB_PATH` directly and is
guarded only by `AGENTCONNECT_MODE`. A ToolConnect that mediates tool calls must account
for it.

**Environment sanitization — wired.** `sanitize_env` (`core/sessions.py`) is
**allowlist-wins**: `BASE_ALLOWLIST` (PATH, HOME, SHELL, TERM, LANG, LC_ALL, USER,
LOGNAME, TMPDIR, TZ), plus `SESSION_VARS`, plus `FORWARDED_CONFIG_VARS` (paths and knobs:
`AGENTCONNECT_DB_PATH`, `AGENTCONNECT_ARTIFACT_DIR`, …). Everything else is dropped
because it was never on the list. `SECRET_DENYLIST` (`core/sessions.py`) and the
`_SECRETISH` regex only police the `AGENTCONNECT_SHELL_ALLOW_ENV` opt-in path.

**CLI mode refusal — wired.** `_refuse_operator_command` (`cli/main.py`) reads
`AGENTCONNECT_MODE`, which `launch` and `shell` set. Under it, `complete` and
`memory promote` are refused with `forbidden_action` and exit code 2; a reviewer may still
run `complete --review`. Empty mode means operator, and nothing is refused.

**Operator-only vs agent-allowed.** Operator: `complete`, `memory promote`, `subtasks
approve|deny`, `launch`, `shell`, session/workspace management, `linear sync`.
Agent: the mode's action list, plus the `bin/ac-context`, `bin/ac-attempt`, `bin/ac-audit`
helper shims (`core/workspace.py`) for harnesses with no MCP client.

## 4. Invocation

Four paths reach the same ledger. Only the first is MCP.

1. **MCP** — tool → `svc.<method>`. The only path with `deniedTools`.
2. **CLI** — `agentconnect …` builds the service from the environment and calls it
   directly. Guarded only by `_refuse_operator_command`.
3. **Shell helper shims** — thin wrappers over the CLI, written into each workspace.
4. **HTTP** — `agentconnect-api`, whose handlers call the service directly, behind
   `enforce`.

Of the four, three authenticate. **The CLI does not**, and that is the remaining soft
spot: it is the transport a managed agent already holds, guarded by an environment
variable it can unset. It is a compliance guard, not a security control, and closing it
needs OS-level isolation rather than another check.

Any ToolConnect that mediates tool calls must mediate **all four** paths, or agents will
route around it through the one it does not cover.

## 5. Auditing

**There is no per-tool-call audit log.** The ledger records domain entities — tasks,
claims, decisions, attempts, reviews, subtasks, artifacts, memory candidates, sessions,
tokens. A tool call is recorded only insofar as it creates one of those rows. An agent
that calls `open_task` or `read_artifact_chunk` leaves no trace of the call.

The completion audit is `core/audit.py`. `audit_task()` (`audit.py:146`) runs required and
advisory checks — the task exists, a workspace exists, a manager session exists, the task
was claimed, an attempt was recorded, changed files in the worktree are registered as
artifacts, subtasks are resolved, reviews completed, decisions recorded, status
consistent. It **writes nothing** (`audit.py:18`), and the service takes care to compute a
fresh handoff without persisting it, because auditing through `get_handoff_summary` would
repair the very staleness it measures (`service.get_handoff_summary`).

If ToolConnect wants a tool-invocation trail, that is a **new** ledger table, not a
reinterpretation of the existing one.

## 6. Safety

Definitive: the safety pipeline scans exactly two surfaces, `artifact_ingest`
(`service.py:449`) and `context_output` (`context.py:594`). **MCP tool inputs and outputs
are not scanned.** A `submit_subtask.instructions`, a `record_decision.decision`, a
`record_attempt.summary`, a `request_review.criteria`, or a `capture_memory_candidate.text`
flows to the ledger unscanned. `subtask_instruction`, `review_input`, and
`attempt_decision_notes` are named in `safety/policies.py` with no policy table, and
`policy()` refuses them rather than guessing.

Tool text is therefore the obvious next safety surface, and it is the natural place for
a tool mediator to stand.

## Proposed division of responsibility

**Move to ToolConnect** — the catalog and the gate:

* Tool definition, versioning, and the single source of truth for the exposed set (which
  would have prevented the `EXPOSED_MCP_TOOLS` drift).
* Discovery, including any dynamic registration — with denylist enforcement moved to
  registration time so the structural guarantee survives.
* Per-call authorization: give `authorize()` a caller. This is the one place where
  AgentConnect has the mechanism and lacks the wiring.
* Tool-invocation audit: who called what, when, with which token.
* Mediation of tool inputs and outputs through a safety surface.

**Stays in AgentConnect** — the meaning of the call:

* `AgentConnectService` remains the only door to the ledger. ToolConnect calls it; it does
  not reach past it.
* Ledger semantics: tasks, attempts, decisions, artifacts, reviews, subtasks.
* The completion audit, and the rule that completion requires it.
* The trust boundary: environment sanitization, `AGENTCONNECT_MODE`, operator-only
  actions, and the principle that **an agent cannot complete its own task**.
* Safety *policy* and enforcement. ToolConnect may become a scanned surface; it does not
  get to decide what a finding means.
* Memory trust labelling, which is BrainConnect's authority, mediated by AgentConnect.

**Neither, yet:** container isolation. AgentConnect is a compliance layer, not a sandbox,
and no tool mediator changes that.

## Preconditions before any code moves

All three are now met, which is why this contract is worth writing against:

1. ~~Fix AC-1.~~ Done: the HTTP adapter authenticates, and completion cannot skip the audit.
2. ~~Reconcile the tool lists.~~ Done: `core.tools.MCP_TOOLS` is the single catalog, and
   `.mcp.json` is generated from it.
3. ~~Decide whether `authorize()` is wired or deleted.~~ Wired, and called by both the HTTP
   and MCP transports.

What remains before ToolConnect could take over the gate: the CLI is unauthenticated, and
tool inputs are still unscanned (below).

## Related

* [BACKPLANE.md](BACKPLANE.md) — the operational contract, five rules.
* [BACKPLANE_SPEC_COMPLIANCE.md](BACKPLANE_SPEC_COMPLIANCE.md) — the compliance spec.
* [MULTI_HARNESS.md](MULTI_HARNESS.md) — router MCP deployment models.
* [INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md) — the defects named above.
* [SAFETY.md](SAFETY.md) — surfaces, policy, engines.
