# agentconnect-temporal

An **optional** Temporal-backed durable-execution substrate for AgentConnect.

The zero-infra SQLite `WorkQueue` is the default and needs no server. This package
is for deployments that **already run a Temporal server**: it rents Temporal's
durable execution, retries, timeouts/heartbeats, and DAG (child workflows), while the
differentiated **privacy×tier authorization stays ours** — reused verbatim from
`agentconnect.common.privacy.admits`.

Nothing in the default AgentConnect path imports this package. See
[`docs/TEMPORAL_FORK.md`](../../docs/TEMPORAL_FORK.md).
