# ADR 0007 — A shipped, fail-closed ToolConnect governor client

Status: accepted (2026-07-12)

## Context

ToolConnect proposed an optional `ToolGovernor` seam
(docs/AGENTCONNECT_CONTRACT.md §3) whose defining property is that its
**unavailability is a denial** — the one place AgentConnect deliberately departs from its
"adapters fail open" discipline. Memory failing open is correct: a missing brain makes an
agent dumber. A policy engine failing open is not a policy engine: a missing authorization
makes an agent *unconstrained*. The failure modes are not symmetric.

ToolConnect ships a stdlib client (`toolconnect.client.ToolConnectClient`), but a shipped
AgentConnect **product** cannot import a sibling repo at runtime. ToolConnect's production
review recorded the absence of an AgentConnect-owned client as Part X item 7
("AgentConnect has a shipped, configurable ToolConnect client"). AgentConnect must own its
own adapter, depending on nothing from the ToolConnect package.

## Decision

Add `core/toolconnect_client.py`, owned by AgentConnect:

- A `ToolGovernor` `Protocol` (the §3 seam) with `authorize` / `record` / `health` and,
  deliberately, **no `invoke`** — AgentConnect's worker runtime stays the only thing that
  runs a tool. The governor is never on the invocation data path.
- `ToolConnectGovernor`, a thin adapter over ToolConnect's HTTP decision API
  (`POST /authorize`, `POST /decisions/{id}/outcome`, `GET /health`) using `httpx` with a
  **lazy import**, exactly like the memory adapters. The transport is injectable for
  testing without a server.
- **Fail closed by construction.** `authorize` returns a `ToolDecision` whose `allowed` is
  `True` only when the server explicitly said so. An unreachable engine, a non-200, an
  unreadable body, or a server announcing an incompatible decision-contract MAJOR all
  resolve to a deny carrying `unavailable=True` — there is no path that returns an allow on
  failure. `unavailable` lets a caller distinguish an *outage* deny from a *policy* deny.
- `record` is best-effort audit (an unreachable server returns `{"recorded": False}`, never
  a crash on the audit path); `health` reports `unreachable` rather than raising.
- Config wiring in `bootstrap.toolconnect_governor_from_env`, with the memory precedence:
  `AGENTCONNECT_TOOLCONNECT_URL` (env) → `toolconnect.base_url` in
  `config/toolconnect.yaml` → no governor (standalone unchanged). Optional token (verbatim
  `Authorization`, never logged) and `mode` (`required` / `advisory`). A malformed block
  degrades to off with a warning.
- A seam on the service: `AgentConnectService.tool_governor` (default `None`) and
  `bind_tool_governor`, mirroring `bind_observability`. `service_from_env` binds the
  governor when configured. An unbound governor changes nothing.

An example `config/toolconnect.yaml` documents the shape.

## Consequences

- AgentConnect can be configured to route every tool authorization through ToolConnect,
  fail-closed, without importing ToolConnect and without ToolConnect importing AgentConnect
  — each depends only on the Protocol/wire contract.
- Standalone AgentConnect is unchanged: no governor is bound unless explicitly configured.
- The one asymmetry (fail-closed vs fail-open) is contained to this adapter and documented
  as intentional; every other adapter's fail-open posture is untouched.
- `advisory` mode, cached `ToolsetPack` resolution, and consulting the governor from the
  worker runtime are out of scope here (no `/resolve_toolset` route exists yet); this ADR
  ships the client and the config/bind seam only.

Regression tests: `tests/test_toolconnect_client.py` — authorize allow/deny over a real
threaded stub server; token sent as `Authorization` (and a 401 fails closed); fail-closed
deny when unreachable; record over the stub and best-effort on outage; incompatible
contract major fails closed; `health` reports unreachable; `from_env` absent → none, env →
built, malformed → off; `service_from_env` binds the governor.
