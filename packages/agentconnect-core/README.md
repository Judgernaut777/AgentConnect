# agentconnect-core

Shared, framework-free core for the AgentConnect Router and Local Model Manager:
data contracts (`agentconnect.common.schemas`), config loaders, the deterministic
task state machine, the SQLite shared memory + artifact store, the quota ledger,
privacy classification + redaction, the provider registry, secret resolution, and
token estimation.

Both services depend on this package; it is the wire contract between them. Depends
only on `pydantic` and `pyyaml`.

See the [repository README](../../README.md) and
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) for the full design.
