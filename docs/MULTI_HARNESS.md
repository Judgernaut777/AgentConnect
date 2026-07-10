# Multi-harness: interchangeable managers, one set of subagents

One control-plane instance, many **interchangeable manager harnesses** (Claude Code,
Codex, Cursor, opencode, …) driving the **same subagents**. Swap which harness you're
using — even mid-task — and work keeps flowing, because the subagents drain a shared
work queue and don't know or care which harness submitted.

**Why it works:** `agentconnect-router` is a standard MCP server (any MCP client speaks
to it), and the whole system coordinates through one shared, multi-writer-safe store
(SQLite + WAL + a lease-fenced work queue). The manager is just the MCP client that
submits tasks and reads compact summaries; the subagents are the runtime workers that
execute them. Decoupled by design.

There are two deployment shapes. They are not mutually exclusive — Model A is a config,
Model B is a transport mode.

## Model A — shared state, a router per harness (works today, zero code)

Each harness spawns its own **stdio** `agentconnect-router`, but they all point at the
**same `AGENTCONNECT_DB`**. Shared tasks, queue, artifacts, and subagents.

Put the same DB path in each harness's MCP config (`.mcp.json` or equivalent):

```json
{ "mcpServers": { "agentconnect": {
  "command": "agentconnect-router",
  "env": { "AGENTCONNECT_DB": "/home/you/.agentconnect/shared_memory.sqlite" }
} } }
```

- ✅ Zero code; works right now. No network surface (stdio child of a trusted harness).
  Per-harness crash isolation.
- ⚠️ Single box only. Each process keeps its own warm state, so two harnesses can
  **double-provision a paid/rented node** (spend). Cross-process writes serialize at the
  SQLite level (`busy_timeout` handles contention).

## Model B — one shared HTTP server (the coherent instance)

Run **one** `agentconnect-router` as an SSE / streamable-HTTP MCP server; every harness
connects to it over the network.

```bash
AGENTCONNECT_MCP_TRANSPORT=streamable-http \
AGENTCONNECT_MCP_HOST=127.0.0.1 \
AGENTCONNECT_MCP_PORT=8760 \
AGENTCONNECT_DB=/home/you/.agentconnect/shared_memory.sqlite \
agentconnect-router
```

Harnesses connect to `http://<host>:8760/mcp` (streamable-HTTP) or `/sse`. Config:

```json
{ "mcpServers": { "agentconnect": { "url": "http://127.0.0.1:8760/mcp" } } }
```

- ✅ One coherent process: clean write serialization, **one** warm rented-node pool (no
  double-provisioning), one routing/eval cache. Supports **remote/distributed** harnesses.
  Warm state survives a manager swap.
- ⚠️ A network endpoint you must secure. It binds `127.0.0.1` by default; **do not expose
  it beyond loopback without TLS + auth** — front it with the same mTLS /
  `ClientIdentityMiddleware` machinery the model-manager uses, or a reverse proxy.
- Single front-door fault domain — but if it restarts, in-flight subagents keep draining
  the queue and reconnecting harnesses resume against the same state.

`AGENTCONNECT_MCP_TRANSPORT` is `stdio` (default) | `sse` | `streamable-http`;
`AGENTCONNECT_MCP_HOST` / `AGENTCONNECT_MCP_PORT` bind the network transports.

## Which to use

Start with **A** to try the workflow for free (all local harnesses, one shared DB).
Move to **B** once you use paid/rented tiers (A risks double-provisioning) or want a
harness on another machine.
