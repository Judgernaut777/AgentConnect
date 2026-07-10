# Integration issues

Defects found while validating AgentConnect's integrations against real BrainConnect and
against its own MCP/HTTP surface. Every entry below was **reproduced**, not inferred; the
reproduction is given so it can be re-run or disputed.

No fix is applied in this document. Three of these involve a design decision that is the
maintainer's to make, and the handoff that produced this file was documentation-scoped.

**None of these were filed as GitHub issues** — the `gh` CLI is not installed in this
environment and no API token is available. This file is the register until they are.

BrainConnect defects are **reported, not patched.** BrainConnect is a separate repository.

---

## AC-1 — Unauthenticated HTTP completion bypasses the audit and the operator gate

**Severity: high.** Defeats operational-contract rules 4 and 5.

`POST /tasks/{task_id}/complete` (`packages/agentconnect-api/.../routes_compliance.py:104`)
has no authentication. The HTTP adapter has none at all — no token check, no dependency,
nothing. The body accepts `force: bool`, and `force=True` skips the completion audit.

Compounding it: `AGENTCONNECT_API_HOST` and `AGENTCONNECT_API_PORT` are in
`FORWARDED_CONFIG_VARS` (`core/sessions.py:122`), so a managed agent is *told* where the
API listens.

If an operator runs `agentconnect-api`, an agent inside `agentconnect shell` can mark its
own task `succeeded`, attributed to any name it likes, without a session token and without
the audit ever running. The CLI refusal (`_refuse_operator_command`) does not apply,
because this is not the CLI.

```python
c = TestClient(create_app())
tid = c.post("/tasks", json={"title": "d", "goal": "g", "created_by": "op"}).json()["id"]
c.post(f"/tasks/{tid}/complete", json={"completed_by": "agent", "force": False})
# -> 403 policy_violation: audit failed          (the guarantee holds)
c.post(f"/tasks/{tid}/complete", json={"completed_by": "agent", "force": True})
# -> 200 {"status": "succeeded"}                 (the guarantee does not)
```

The non-`force` path is sound: the audit gates it. The `force` path is an operator escape
hatch reachable by anyone who can open a socket.

Docs that currently assert otherwise, and are corrected in this commit:
`docs/OPERATOR_GUIDE.md` ("MCP and HTTP deny it structurally") and `docs/BACKPLANE.md`.

**Options**, none taken here: authenticate the HTTP adapter; or drop `force` from the HTTP
surface and keep it CLI-only; or bind the adapter to a Unix socket the sanitized
environment cannot reach; or stop forwarding `AGENTCONNECT_API_*` into agent shells.

---

## AC-2 — `authorize()` is defined, tested, and called by nothing

**Severity: medium (documentation asserts a guarantee that no code provides).**

`AgentConnectService.authorize(token, action)` (`core/service.py:1531`) validates a session
token against its scoped action list and raises `PolicyViolation`. Outside `tests/`, it has
**zero call sites** across every shipped package:

```sh
grep -rn "\.authorize(" packages/ --include=*.py | grep -v build/lib   # -> no results
```

The MCP server, the CLI, and the HTTP API all build a service from the environment and
invoke it with no token and no scope check. The `act_` token is genuinely minted, scoped,
SHA-256-hashed, and revoked when the shell exits — it just never gates a call.

The *outcomes* the docs promise still hold today, by other means: `complete_task` is not an
MCP tool, and the CLI refuses it under `AGENTCONNECT_MODE`. But they hold structurally, not
because the token was checked. `docs/BACKPLANE.md`'s "Manager mode buys ten actions,
reviewer mode six" describes data, not enforcement.

Decide: wire it, or delete it. An uncalled security mechanism reads as a guarantee to the
next person, and is the more dangerous of the two states.

---

## AC-3 — `.mcp.json` `allowedTools` does not match the registered tools

**Severity: medium (functional).**

`EXPOSED_MCP_TOOLS` (`core/workspace.py:46`) is written into every workspace's `.mcp.json`
as `allowedTools` (`workspace.py:312`). The MCP server registers 17 tools. The lists differ:

* **Advertised but nonexistent:** `get_subtask_status`. No such tool is registered; the
  real one is `get_status`. The phantom name also appears in `MANAGER_ACTIONS` and
  `READONLY_ACTIONS` (`core/sessions.py:36`).
* **Registered but not advertised:** `capture_memory_candidate`, `create_task`,
  `explain_route`, `get_handoff_summary`, `get_status`, `open_task`, `recall_memory`,
  `record_memory_feedback`.

A harness that honors `allowedTools` therefore denies a manager every memory tool and the
handoff summary. Reproduce by comparing the tuple against `@mcp.tool()` registrations in
`packages/agentconnect-mcp/src/agentconnect/mcp/server.py`.

`DENIED_MCP_TOOLS` is **correct** — none of the forbidden six is registered by either
server. The denial is structural.

Related, cosmetic: `mcp/server.py:3` claims "exactly the thirteen tools a manager needs";
there are 17.

---

## AC-4 — BrainConnect's promotion safety gate is invisible to AgentConnect's adapter

**Severity: medium. Contract drift, introduced by BrainConnect `b128e65`.**

BrainConnect grew a second safety gate at promotion, after AgentConnect's adapter was
written. Its `promote` now raises `wiki.candidates.SafetyRefused` when a candidate carries
a medium-or-higher secret, a high-risk injection or tool-control payload, or was
quarantined at capture — and it offers reviewers a `safety_override` / `override_reason`
escape hatch:

```python
>>> inspect.signature(wiki.api.promote)
(repo, candidate_id, reviewer, confidence, scope=None, reviewer_type='human',
 note=None, safety_override=False, override_reason=None)
>>> hasattr(wiki.candidates, "SafetyRefused")
True
```

AgentConnect's `WikiBrainMemoryAdapter.promote_candidate` (`core/memory.py:449`) knows none
of it: `grep` for `SafetyRefused`, `safety_override`, `override_reason` in `memory.py`
returns nothing. A safety refusal surfaces as a bare exception in-process, or as an
undifferentiated `httpx.HTTPStatusError` over the wire. AgentConnect cannot tell a safety
refusal from a network fault, cannot surface the finding summary to the human at the gate,
and offers no path to the legitimate override.

Fails when: a human promotes a candidate whose text contains a live credential or a
high-risk injection. Previously a valid promotion; now refused, opaquely.

**Report to BrainConnect? No — this one is AgentConnect's to absorb.** BrainConnect's
behavior is correct and deliberate. The adapter should model the refusal.

Dead code worth removing while there: `memory.py:441` downgrades a `promoted` status
returned by capture. BrainConnect's `candidates.create_checked` writes `status='pending'`
unconditionally; no argument makes it return `promoted`. The guard is harmless but can
never fire against real BrainConnect.

---

## AC-5 — Capture with no origin actor raises `TypeError` instead of a clean error

**Severity: low.**

AgentConnect's `CaptureRequest.origin_actor_id` defaults to `None`
(`core/memory.py:186`) and the adapter forwards it verbatim (`memory.py:434`).
BrainConnect's `_as_capture_request` skips `None` values, and its own `CaptureRequest`
requires `proposed_by`:

```python
>>> wiki.api._as_capture_request({"text": "x", "origin_actor_id": None, ...})
TypeError: CaptureRequest.__init__() missing 1 required positional argument: 'proposed_by'
```

A capture with an unset origin actor dies with a `TypeError` rather than a typed
`ApiError`. Not hit by the worker runtime, which goes through MCP `brain_capture` and
defaults `proposed_by=harness`.

Fix belongs on AgentConnect's side: validate before the call and raise with the field
name. Do **not** invent a default actor — that forges provenance on a memory claim.

---

## AC-6 — Two services documented on the same port

**Severity: low (documentation).**

`docs/BACKPLANE_SPEC_COMPLIANCE.md:146` tells operators to set
`AGENTCONNECT_API_URL=http://localhost:8787`. `core/bootstrap.py:39` defaults `WIKIBRAIN_URL`
to `http://localhost:8787`. Follow both defaults and the HTTP adapter and BrainConnect
contend for one port.

Moot today only because BrainConnect ships no HTTP server (below).

---

## AC-7 — BrainConnect still has no HTTP server; the adapter still defaults to one

**Severity: low. Known, long-standing, not new drift.**

`WikiBrainMemoryAdapter` defaults to `http://localhost:8787` (`core/memory.py:362`,
`bootstrap.py:39`). BrainConnect ships **no HTTP server**: its only `serve` is
`wiki mcp serve`, which is stdio. BrainConnect's own `docs/STATUS.md` states it outright.

Consequently nothing exercises this contract on the wire. `tests/test_wikibrain_integration.py`
injects a transport that dispatches into `wiki.api` in-process (real semantics, no wire),
and `tests/test_agent_loop_e2e.py` runs a real HTTP server serving canned responses (real
wire, no semantics). No test has both halves — as `docs/STATUS.md` already says.

A deployment that lets the adapter fall through to `httpx` gets connection-refused.
`wiki serve` belongs to BrainConnect; **do not build it from this side.**

---

## Not a bug: `MODEL_BACKEND_API_KEY` is absent from `SECRET_DENYLIST`

Worth recording because it looks like a hole and is not.

`sanitize_env` (`core/sessions.py:145`) is **allowlist-wins**. A variable reaches the agent
only if it is in `BASE_ALLOWLIST`, `SESSION_VARS`, or `FORWARDED_CONFIG_VARS`.
`SECRET_DENYLIST` polices only the `AGENTCONNECT_SHELL_ALLOW_ENV` opt-in path. Verified:

```python
sanitize_env({"MODEL_BACKEND_API_KEY": "sk-leak", "OPENAI_API_KEY": "sk-2", ...},
             {"AGENTCONNECT_MODE": "manager"})
# -> {'AGENTCONNECT_MODE', 'HOME', 'PATH'}
```

Adding the name to the denylist would be defense in depth, not a fix.

---

## Reproduction environment

`origin/main`, gate `821 passed, 3 skipped` (`824 passed` with the `safety-secrets`
extra installed). BrainConnect checked out at `b128e65`.
