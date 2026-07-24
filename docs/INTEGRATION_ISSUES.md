# Integration issues

Defects found while validating AgentConnect's integrations against real BrainConnect and
against its own MCP/HTTP surface. Every entry below was **reproduced**, not inferred; the
reproduction is given so it can be re-run or disputed. Defects found in the *sibling*
repositories during the 2026-07 ecosystem review are registered separately, in
[ECOSYSTEM_FINDINGS.md](ECOSYSTEM_FINDINGS.md).

**AC-1 through AC-6 are now closed**, each with a test that fails against the old code.
AC-7 — the one entry that was never AgentConnect's to close — has since been closed
upstream by BrainConnect. Status is recorded per issue below rather
than by deleting the entry: a defect register that forgets what was wrong cannot tell you
whether the fix still holds.

**None of these were filed as GitHub issues** — the `gh` CLI is not installed in this
environment and no API token is available. This file is the register.

BrainConnect defects are **reported, not patched.** BrainConnect is a separate repository.

---

## AC-1 — Unauthenticated HTTP completion bypasses the audit and the operator gate

**Severity: high. Status: CLOSED.** Defeated operational-contract rules 4 and 5.

**Fix.** `enforce` is an app-level dependency: every route but `GET /health` resolves a
bearer token and calls `service.authorize()`. `force` was removed from `CompleteBody`
entirely; `POST /tasks/{id}/complete/override` is a separate operator-only action that
requires a reason and records it as a locked decision before completing. Completion is
attributed to the authenticated principal, never to a body field.
`tests/test_http_authorization.py` runs a real `uvicorn` server on a real port, holds a
real managed-session token, and replays every step of the original bypass.

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

**Severity: medium. Status: CLOSED.** Documentation asserted a guarantee no code provided.

**Fix.** The HTTP adapter calls `authorize()` on every route; the MCP server wraps every
tool registration and calls it with the tool's declared action. `authorize()` gained scope
binding (`task_id` / `review_id`) and a mode-aware denial: `NEVER_TOKEN_ACTIONS` refuses
every token including the operator's, `AGENT_FORBIDDEN_ACTIONS` refuses every managed
agent. Unknown, expired, and revoked tokens now raise `Unauthenticated` (401) rather than
`PolicyViolation` (403) — *who are you* and *you may not* are different failures.

The CLI still does not authenticate, by design, and `docs/BACKPLANE.md` says so.

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

**Severity: medium (functional). Status: CLOSED.**

**Fix.** `core/tools.py` is now the single catalog. `workspace.EXPOSED_MCP_TOOLS` is
generated from it, `agentconnect-mcp` imports it, and `tests/test_mcp_catalog.py` asserts
the server registers exactly the catalog, that every advertised name resolves to a real
tool, and that every tool's action is one a manager actually holds. The parallel
`phantom_routes()` check does the same job for the HTTP route table.

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

**Severity: medium. Status: CLOSED.** Contract drift, introduced by BrainConnect `b128e65`.

**Fix.** `MemorySafetyRefused` carries BrainConnect's audit-safe summary and is raised for
both transports — the in-process shim's `SafetyRefused` is recognized structurally (it
cannot be imported: `wiki` is an optional peer), and the HTTP path by its error code.
`MemoryUnavailable`, `MemoryServerError`, `MemoryAuthorizationError`, and `InvalidRequest`
cover the rest; an unrecognized failure is re-raised untouched rather than relabelled.
`promote_candidate` accepts `safety_override` + `override_reason` and refuses the first
without the second. AgentConnect never sets the override on its own behalf.

One scope note on that override, now that a real wire exists: BrainConnect's HTTP
surface (`brainconnect serve`) refuses any promote payload carrying
`safety_override`/`override_reason` with `403 forbidden` **by design** — the override is
human-only, at BrainConnect's own CLI (`brainconnect promote --safety-override
--override-reason …`) — so over HTTP the forwarded override surfaces as a
`MemoryAuthorizationError`, not a `MemorySafetyRefused`. The adapter's override path
works only via an in-process/injected transport.

Per-item `safety` survives recall on `MemoryItem.safety`; `quarantined` and `safety`
survive capture on `CaptureResult`. Quarantine is a field, never inferred from `message`.
Safety cannot set `trusted`: `tests/test_memory_safety_metadata.py` pins a flagged-but-
trusted claim and a clean-but-untrusted one.

These are exactly the two observability gaps BrainConnect's own `docs/INTEGRATIONS.md`
flagged against `9503661` (before this closed them). BrainConnect has since published a
formal contract — `docs/CONTRACT.md` and seven `tests/contract/*.json` fixtures — pinning
the `safety`, `quarantined`, and refusal shapes. `tests/test_brainconnect_contract.py`
holds this adapter to those fixtures and cross-checks the sibling repo when it is present.

The dead `status == "promoted"` downgrade in `capture_candidate` was left in place: it
cannot fire against real BrainConnect, but it is the adapter's contract with *any* backend
that claims a capture promoted something.

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

**Severity: low. Status: CLOSED.**

**Fix.** `WikiBrainMemoryAdapter.capture_candidate` refuses an empty `origin_actor_id`
with `InvalidRequest`, naming its own field, before the call leaves the process. No
default actor is invented — that would forge the provenance of a memory claim.

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

**Severity: low (documentation). Status: CLOSED** — the example now uses `:8130`.

`docs/BACKPLANE_SPEC_COMPLIANCE.md:146` tells operators to set
`AGENTCONNECT_API_URL=http://localhost:8787`. `core/bootstrap.py:39` defaults `WIKIBRAIN_URL`
to `http://localhost:8787`. Follow both defaults and the HTTP adapter and BrainConnect
contend for one port.

When filed, this was moot in practice because BrainConnect shipped no HTTP server.
It no longer is: `brainconnect serve` now exists and defaults to `127.0.0.1:8787`
(below), so the corrected `:8130` example is load-bearing, not cosmetic.

---

## AC-7 — BrainConnect had no HTTP server; the adapter defaulted to one

**Severity: low. Status: CLOSED — fixed upstream by BrainConnect.**

**Fix (theirs, not ours).** BrainConnect now ships `brainconnect serve`
(`cli/brainconnect/server.py`): an HTTP transport defaulting to `127.0.0.1:8787` —
exactly the adapter's default — with bearer auth via `--token`/`BRAINCONNECT_TOKEN`,
serving precisely the routes `WikiBrainMemoryAdapter` calls (`/recall`, `/capture`,
`/candidates/{id}/promote`, `/candidates`, `/feedback`, `/registry`, `/health`).
BrainConnect's own
gate wire-tests it, cross-checked against this adapter. A deployment that lets the
adapter fall through to `httpx` now reaches a real server instead of
connection-refused.

As filed: `WikiBrainMemoryAdapter` defaults to `http://localhost:8787` and BrainConnect
shipped **no HTTP server** — its only `serve` was `wiki mcp serve`, which is stdio — so
nothing exercised this contract on the wire.

Residual gap, deliberately left: no AgentConnect test yet exercises real semantics over
the real wire. `tests/test_wikibrain_integration.py` dispatches into the sibling's API
in-process (real semantics, no wire) and `tests/test_agent_loop_e2e.py` runs a real HTTP
server serving canned responses (real wire, no semantics). The both-halves test is newly
unblocked by `brainconnect serve`, but it is new test infrastructure, not a correction,
so it is not added under the stabilization freeze.

---

## AC-8 — Refusal-envelope shape was ambiguous across the two repos

**Severity: low (latent, no wire path exists). Status: RESOLVED by tolerating both
shapes.** Found validating against BrainConnect's `docs/CONTRACT.md` (`e75cb83`).

The refusal envelope `brainconnect serve` would return (then still unbuilt) was described two ways
at once. BrainConnect's `docs/CONTRACT.md` documented a **nested** body —
`{"error": {"code": "safety_refused", "safety": {…}}}` at HTTP 409 — while its server
intent (and the flat fixture in its working tree) is **flat**: `error` is the code
string, `safety` is top-level. BrainConnect chose flat deliberately, to match this
adapter's *original* reader, which compared `body["error"]` to `"safety_refused"`.

So there was never a shipped defect: against the flat shape BrainConnect actually
intended at the time, the original adapter was correct, and no `brainconnect serve`
existed yet to exercise either shape. The risk was purely that the two repos could drift — one
side changing the nesting would silently turn a safety refusal into an
`invalid_request`, the exact trust-versus-retry confusion the taxonomy exists to
prevent.

**Resolution.** `_envelope()` (`core/memory.py`) now reads **both** shapes: a nested
`error.code` / `error.safety`, or a flat `error` string with top-level `safety`. Then
`_classified` maps BrainConnect's five codes (`safety_refused`, `not_found`,
`forbidden`, `invalid_request`, `backend_error`) to the typed memory errors before
falling back to bare status — and a 409 is never read as a safety refusal *without*
its code, because without the code we cannot know it is one.
`tests/test_brainconnect_contract.py` parametrizes the refusal over flat and nested,
and cross-checks BrainConnect's `promotion_safety_refusal.json` shape-tolerantly, so
whichever shape BrainConnect ships cannot break this adapter and cannot go unnoticed.

**Cross-repo note (not AgentConnect's to fix):** at the time of writing, BrainConnect's
committed `docs/CONTRACT.md` (nested) and its working-tree `errors.py` + fixture (flat)
disagree. That is BrainConnect's to reconcile; this adapter is correct either way. No
GitHub issue was filed — `gh` is unavailable here — so it is recorded only as this note.

*2026-07-24:* BrainConnect has since reconciled on the **nested** shape — its
`errors.py` `envelope()`, its `tests/contract/promotion_safety_refusal.json` fixture,
and its `docs/CONTRACT.md` now agree, and `brainconnect serve` answers every refusal
with `errors.envelope(exc)`. The adapter's flat-shape reading remains for defensiveness
only.

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

## New: the tests that keep these closed

* `tests/test_http_authorization.py` — 21 tests. Missing/malformed/unknown/revoked tokens,
  cross-task scope, manager and reviewer completion denial, `force` unavailable to managed
  tokens, override reason required and recorded, impersonation refused, and the original
  bypass replayed against a real HTTP server on a real port.
* `tests/test_mcp_catalog.py` — 11 tests. The catalog is the source of truth; the token
  gates MCP tool calls; a revoked token stops a tool mid-session; a tool cannot reach
  another task.
* `tests/test_memory_safety_metadata.py` — 22 tests. Safety survives; trust is untouched;
  the four failure modes are told apart.
* `tests/test_brainconnect_contract.py` — 12 tests. AgentConnect's adapter against
  BrainConnect's pinned recall/capture/refusal fixtures; both envelope shapes; a
  shape-tolerant cross-check of the real sibling-repo fixtures when present.

## Reproduction environment

`origin/main`, gate `888 passed, 3 skipped` (`891 passed` with the `safety-secrets`
extra installed). BrainConnect checked out at `e75cb83` (its `docs/CONTRACT.md` and
`tests/contract/` fixtures).
