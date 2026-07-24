# Memory federation: workers write, only the manager reads

Durable cross-task memory for the agent fleet — **without** giving a lower-tier or
remote worker any way to read privileged memory back. Memory flows **one way, up the
trust gradient**, so the privacy problem is dissolved by topology instead of by a new
per-tier access model.

The memory store is [BrainConnect](https://github.com/Judgernaut777/BrainConnect) — a
human-gated knowledge base exposed over MCP (`brain_capture` to write, `brain_recall` /
`brain_search` / `brain_graph` / `brain_hybrid` to read). It is **optional**; AgentConnect
runs without it. BrainConnect was renamed from *WikiBrain*; the installed console
script is `brainconnect` (the old `wiki` entry point no longer ships —
`McpStdioMemorySink` auto-discovers `brainconnect` and falls back to `wiki` only when
no command is given).

## The one-way rule

- **Capture = write-only, any worker → BrainConnect.** A worker only ever sends data it
  *already holds*, so a write leaks nothing. Even a remote or untrusted worker may
  contribute its own findings. Each capture carries provenance (`task_id`,
  `agent_type`, `privacy_class`) embedded in the payload, and lands as **unvetted
  pending material** behind BrainConnect's morning human gate — it never auto-promotes to
  truth.
- **Recall = the manager only.** The single highest-trust entity — the orchestrator that
  already sees every task — is the only reader. Peers and lower-tier workers have **no
  recall path**, so lateral or downward leakage is *structurally impossible*. No
  `may_claim`-style tier check has to be built inside BrainConnect.
- **Re-injection uses the existing privacy pass.** When the manager hands remembered
  context *down* into a delegated subtask, it flows through the router's existing
  classify/redact-on-submit path. The manager, already the privacy authority, governs
  exactly what memory reaches each subagent.

**Consequence:** memory sharpens the *manager's orchestration* (redacted down into tasks
as appropriate), not each worker's raw execution. Usually what you want, and it removes
the hardest part of a symmetric design.

## Two faces of the same brain

BrainConnect gates its MCP tools by mode (`brainconnect mcp serve [--read-only|--contribute-only]`):

| Face | Command | Tools exposed |
|---|---|---|
| **Worker-facing** | `brainconnect mcp serve --contribute-only` | `brain_capture` **only** — write-only, no recall |
| **Manager-facing** | `brainconnect mcp serve` (or `--read-only`) | full recall (`brain_recall` / `search` / `graph` / `hybrid`) |

`--contribute-only` is the inverse of the existing `--read-only` mode; the two flags are
mutually exclusive. The worker fleet points at the contribute-only server; the manager
keeps its own recall-capable server.

## The runtime seam

The worker runtime is not otherwise an MCP client — it has a fixed local tool set. Memory
is wired through one injectable seam so a worker can *write* memory without gaining any
other outbound capability:

- **`remember` action.** Advertised in the system prompt **only** when
  `RuntimeConfig.allow_memory` is set:

  ```json
  {"action": "remember", "text": "<a durable finding worth keeping>"}
  ```

  The prompt states plainly: *writes to shared memory; you cannot read it back.*
- **`MemorySink` protocol** (`runtime/memory.py`). `capture(text, *, provenance) -> str`.
  Concrete implementations:
  - `McpStdioMemorySink` — a persistent stdio MCP client to a contribute-only BrainConnect.
    One background event loop holds one session, so a long-lived worker pays the
    subprocess/handshake cost once and every `remember` reuses it.
  - `NullMemorySink` — the default when nothing is wired; `capture` returns an
    `"ERROR: ..."` string.
- **Fails soft.** A memory outage never breaks task execution: `capture` returns an
  `"ERROR: ..."` string exactly like a tool observation, and the loop continues. No brain
  reachable → workers run stateless, today's behavior.
- **Double-gated.** `remember` does nothing unless `allow_memory` is on **and** a sink is
  injected; either missing → the action reports disabled and no write occurs.

## Wiring it up

```python
from agentconnect.runtime import (
    LangGraphAgentRuntime, McpStdioMemorySink, RuntimeConfig,
)

sink = McpStdioMemorySink(
    command="brainconnect",
    args=["mcp", "serve", "--contribute-only"],
    cwd="/path/to/your/brainconnect/repo",
    harness="agentconnect",
)
runtime = LangGraphAgentRuntime(
    model_source,
    RuntimeConfig(workspace_root="...", allow_memory=True),
    memory_sink=sink,
)
```

The manager, separately, adds a **recall-capable** BrainConnect (`brainconnect mcp serve`)
to its own MCP client config — never exposed to workers.
