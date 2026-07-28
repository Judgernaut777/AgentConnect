# Setup integration contract

**How the Connect control plane configures the Work plane during onboarding — and the
boundaries that hold while it does.**

> **Status: design direction, not shipped behavior.** AgentConnect `0.1.0` has the primitives
> setup configures — workspaces, projects, scoped session tokens, the task/artifact/decision/
> review/handoff ledger, routing, and the single-global budget — but it has **no organization
> object, no owner field on those resources, and no onboarding flow**, and the Connect
> control-plane application that would drive this contract is not yet built
> ([Connect ADR 0002](https://github.com/Judgernaut777/Connect/blob/main/docs/adr/0002-control-plane-repository-boundary.md)).
> This document states the contract the two planes are built toward.

---

## What setup configures in the Work plane

Both the [human-guided](https://github.com/Judgernaut777/Connect/blob/main/docs/SETUP_HUMAN_GUIDED.md)
and [agent-led](https://github.com/Judgernaut777/Connect/blob/main/docs/SETUP_AGENT_LED.md)
flows resolve, for the Work plane, to configuring these primitives:

| Setup input | Work-plane primitive |
|---|---|
| Harness selection & installation | Registered manager harnesses (interchangeable executors) |
| Workspace isolation level | Required isolation level + enforcement provider ([WORKSPACE_ISOLATION.md](WORKSPACE_ISOLATION.md)) |
| Users, agents, roles | Principals recorded in the ledger (AgentConnect records *who acted*; it does not own the identity directory) |
| Projects & workspaces | Workspaces, projects, workspace templates, delegated workspace administration |
| Budgets & delegations | The [budget model](BUDGET_MODEL.md) — enforced against work |
| Security profile | Scoped `act_` tokens, completion/audit gates, approval thresholds |

## The contract, and its boundaries

1. **Proposal, then scoped approval.** Setup (human or agent) proposes a Work-plane
   configuration; it is applied only after scoped human approval, under temporary grants that
   are revoked when setup finishes ([Connect SETUP_AGENT_LED.md](https://github.com/Judgernaut777/Connect/blob/main/docs/SETUP_AGENT_LED.md)).
2. **Ownership is explicit.** Joining an organization does not silently transfer a personal
   workspace, project, or token. Every such resource gains an explicit owner; transfers are
   previewed, never implicit ([ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md)).
3. **Identity stays attributable.** Stable internal identities are preserved so historical
   task, artifact, decision, and audit records remain attributable after migration.
4. **The audit gate is not bypassable.** No setup step, organizational role, or setup agent may
   let a managed session complete its own task or skip the audit — the property holds at any
   organizational scale.
5. **The control plane stays thin.** Setup records and proposes; the Work plane enforces.
   Connect does not become a second place where Work-plane authorization is decided.

## See also

- [ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md) — the Work plane's part in org-aware onboarding.
- [WORKSPACE_ISOLATION.md](WORKSPACE_ISOLATION.md) — the isolation level setup selects.
- [BUDGET_MODEL.md](BUDGET_MODEL.md) — the budgets setup configures.
- [Connect docs/ORGANIZATION_MODEL.md](https://github.com/Judgernaut777/Connect/blob/main/docs/ORGANIZATION_MODEL.md) — the org model this contract serves.
