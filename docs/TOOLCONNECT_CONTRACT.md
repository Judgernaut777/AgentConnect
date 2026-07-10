# ToolConnect — integration contract

**Status: design note. ToolConnect does not exist.** No code moves as a result of this
document. AgentConnect exposes a fixed MCP tool set today and runs standalone.

This is an inventory of AgentConnect's tool surface as it actually is — including three
places where the code and the documentation disagree — followed by a proposed division of
responsibility. An integration contract written against the documented behavior rather
than the real behavior would be wrong on arrival, so the real behavior is what is written
here.

## The boundary in one sentence

**ToolConnect would own the tool *catalog*: what tools exist, how they are discovered, how
a call is authorized. AgentConnect keeps the *consequences*: what a call means for the
ledger, and whether the work it recorded is enough to complete a task.**

## 1. MCP integration points

There are **two** MCP servers in this repository, and they are not interchangeable.

| Server | Package | Entry point | Transport | Role |
|---|---|---|---|---|
| `agentconnect` | `agentconnect-mcp` | `mcp/server.py:432` | stdio (default), sse, streamable-http on `:8765` | The compliance surface a managed agent sees |
| `agentconnect-router` | `agentconnect-router` | `router/mcp_server.py:540` | stdio, or shared on `:8760` | Routing, budget, federated work queue |

Only the first is written into a managed workspace's `.mcp.json` (`core/workspace.py:297`).
The forbidden-tool guarantee below concerns that server.

`build_mcp_server()` (`mcp/server.py:80`) registers **17** tools. Its own docstring says
"exactly the thirteen tools a manager needs" (`server.py:3`). The docstring is wrong; the
count is not load-bearing anywhere, but it is the first sign that this list has drifted.

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
`DENIED_MCP_TOOLS` (`workspace.py:53`), written into `.mcp.json` as `deniedTools` so the
denial is auditable, and independently listed in `FORBIDDEN_ACTIONS` (`sessions.py:64`).

Verified: none of the six is registered by either MCP server. The denial is **structural**
— no server offers them — and the denylist is a statement of intent, not the mechanism.
A ToolConnect that introduced dynamic registration would convert this from a structural
guarantee into a policy check, which is a real loss. If it must, the denylist has to
become an enforced filter at registration time, not a hint in a config file.

## 2. Tool discovery

Static registration only. Tools are `@mcp.tool()`-decorated functions resolved at
server-build time; there is no registry table and no plugin path. Adding a tool means
editing `server.py`.

An agent learns the tool set three ways, all static: the MCP `tools/list` response, the
per-workspace `.mcp.json` (`workspace.py:297`), and the injected `AGENTCONNECT.md` /
`CLAUDE.md` / `CODEX.md` instruction files (`workspace.py:394`).

**Defect (verified).** `EXPOSED_MCP_TOOLS` (`workspace.py:46`) is what `.mcp.json` advertises
as `allowedTools`. It does not match the server:

* It names `get_subtask_status`, **which is not a registered tool.** The real tool is
  `get_status`. The same phantom name appears in `MANAGER_ACTIONS` and `READONLY_ACTIONS`
  (`sessions.py:36`).
* It omits eight tools the server does register, including `recall_memory`,
  `get_handoff_summary`, `capture_memory_candidate`, and `create_task`.

So a harness that honors `allowedTools` denies a manager the memory tools, and the
allowlist grants one tool that cannot be called. This is exactly the kind of drift a
catalog owner exists to prevent, and it is the strongest argument in this document for
ToolConnect owning the catalog. See [INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md).

## 3. Permissions

Three mechanisms, of which **only two are wired**.

**Scoped session tokens — defined, not enforced.** `mint_token()` (`sessions.py:225`)
issues `act_<urlsafe>`; only its SHA-256 is stored (`storage.py:152`). `build_scope()`
(`sessions.py:233`) records the mode's action list — manager 10, reviewer 6, readonly 4.
`AgentConnectService.authorize(token, action)` (`service.py:1531`) checks expiry,
revocation, `FORBIDDEN_ACTIONS`, and scope, raising `PolicyViolation`.

**`authorize()` is called by no adapter.** Not by the MCP server, not by the CLI, not by
the HTTP API — only by tests. Enforcement today is structural and environmental:
`.mcp.json` allow/deny, environment sanitization, and the CLI mode refusal below. The
token is real, scoped, hashed, and revoked on shell exit; it simply gates nothing. Any
ToolConnect design that assumes per-call token authorization is designing a feature, not
adopting one.

**Environment sanitization — wired.** `sanitize_env` (`sessions.py:145`) is
**allowlist-wins**: `BASE_ALLOWLIST` (PATH, HOME, SHELL, TERM, LANG, LC_ALL, USER,
LOGNAME, TMPDIR, TZ), plus `SESSION_VARS`, plus `FORWARDED_CONFIG_VARS` (paths and knobs:
`AGENTCONNECT_DB_PATH`, `AGENTCONNECT_ARTIFACT_DIR`, …). Everything else is dropped
because it was never on the list. `SECRET_DENYLIST` (`sessions.py:80`) and the
`_SECRETISH` regex only police the `AGENTCONNECT_SHELL_ALLOW_ENV` opt-in path.

**CLI mode refusal — wired.** `_refuse_operator_command` (`cli/main.py:787`) reads
`AGENTCONNECT_MODE`, which `launch` and `shell` set. Under it, `complete` and
`memory promote` are refused with `forbidden_action` and exit code 2; a reviewer may still
run `complete --review`. Empty mode means operator, and nothing is refused.

**Operator-only vs agent-allowed.** Operator: `complete`, `memory promote`, `subtasks
approve|deny`, `launch`, `shell`, session/workspace management, `linear sync`.
Agent: the mode's action list, plus the `bin/ac-context`, `bin/ac-attempt`, `bin/ac-audit`
helper shims (`workspace.py:287`) for harnesses with no MCP client.

## 4. Invocation

Four paths reach the same ledger. Only the first is MCP.

1. **MCP** — tool → `svc.<method>`. The only path with `deniedTools`.
2. **CLI** — `agentconnect …` builds the service from the environment and calls it
   directly. Guarded only by `_refuse_operator_command`.
3. **Shell helper shims** — thin wrappers over the CLI, written into each workspace.
4. **HTTP** — `agentconnect-api`, whose handlers call the service directly.

**The HTTP adapter has no authentication whatsoever, and it exposes completion.**
`POST /tasks/{task_id}/complete` (`routes_compliance.py:104`) accepts `force: bool`, and
`force=True` skips the audit. Meanwhile `AGENTCONNECT_API_HOST` and `AGENTCONNECT_API_PORT`
are forwarded into the agent's environment (`sessions.py:122`). If an operator runs the
HTTP adapter, a managed agent is told where it is and can mark its own task `succeeded`
without a token and without passing the audit. This defeats two of the five rules in the
[operational contract](BACKPLANE.md). It is filed in
[INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md) as AC-1.

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
repair the very staleness it measures (`service.py:1556`).

If ToolConnect wants a tool-invocation trail, that is a **new** ledger table, not a
reinterpretation of the existing one.

## 6. Safety

Definitive: the safety pipeline scans exactly two surfaces, `artifact_ingest`
(`service.py:449`) and `context_output` (`context.py:594`). **MCP tool inputs and outputs
are not scanned.** A `submit_subtask.instructions`, a `record_decision.decision`, a
`record_attempt.summary`, a `request_review.criteria`, or a `capture_memory_candidate.text`
flows to the ledger unscanned. `subtask_instruction`, `review_input`, and
`attempt_decision_notes` are named in `safety/policies.py:26` with no policy table, and
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

1. Fix AC-1 (unauthenticated HTTP completion). A mediator in front of MCP is theatre while
   the HTTP door is open.
2. Reconcile `EXPOSED_MCP_TOOLS`, `MANAGER_ACTIONS`, and the registered tool names — decide
   whether `get_subtask_status` should exist or `get_status` should be renamed.
3. Decide whether `authorize()` is wired or deleted. A defined-but-uncalled security
   mechanism is worse than an absent one, because it reads as a guarantee.

## Related

* [BACKPLANE.md](BACKPLANE.md) — the operational contract, five rules.
* [BACKPLANE_SPEC_COMPLIANCE.md](BACKPLANE_SPEC_COMPLIANCE.md) — the compliance spec.
* [MULTI_HARNESS.md](MULTI_HARNESS.md) — router MCP deployment models.
* [INTEGRATION_ISSUES.md](INTEGRATION_ISSUES.md) — the defects named above.
* [SAFETY.md](SAFETY.md) — surfaces, policy, engines.
