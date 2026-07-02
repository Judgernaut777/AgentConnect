# agentconnect-router

The **Agent Router** — AgentConnect's MCP control plane and **primary product**.
Classifies tasks, enforces privacy/quota policy, resolves capability profiles,
scores providers deterministically, dispatches work, and returns compact summaries
+ artifact references (context virtualization).

Runs **standalone**: with no Local Model Manager it is a full cloud-routing +
policy + memory engine. When the optional `agentconnect-model-manager` package is
installed (extra `embedded`) or a `MODEL_MANAGER_URL` is configured, it also routes
to local inference nodes over **mutual TLS** — no shared secret on the wire.

```
pip install agentconnect-router            # standalone
pip install "agentconnect-router[embedded]" # + in-process manager for single-box dev
agentconnect-router                         # stdio MCP server
```

See the [repository README](../../README.md) and
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).
