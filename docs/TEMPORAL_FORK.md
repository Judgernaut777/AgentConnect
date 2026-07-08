# Temporal fork → its own project

The optional Temporal-backed durable-execution substrate has been **extracted into its
own repository**: **[`agentconnect-temporal`](https://github.com/Judgernaut777/agentconnect-temporal)**.

**Why it's a separate project:** the zero-infra SQLite `WorkQueue` is AgentConnect's
default and needs no server. Temporal is an *optional* alternative backend for deployments
that already run a Temporal server — so it lives on its own release cadence and never weighs
on the zero-infra core. It rents Temporal's commodity mechanics (durable execution, retries,
timeouts/heartbeats, child-workflow DAG) while the differentiated **privacy×tier
authorization stays here** — the fork reuses `agentconnect.common.privacy.admits` verbatim,
enforced as an admission activity *before* any execution.

That shared predicate (`common/privacy.py::admits` / `allowed_tiers` /
`admissible_classes`) remains the single source of truth this repo's `WorkQueue.may_claim`
delegates to; the extraction did not change it.

See the extracted repo's `docs/TEMPORAL_FORK.md` for the full design (topology, activities,
per-tier task queues).
