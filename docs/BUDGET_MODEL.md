# Budget model

**Budgeting is not merely restriction. Organizations set boundaries; teams decide how to use
what they are allocated. The budget model must express both — from one person's `$20/day` to a
company's delegated departmental allocations — as the same primitives.**

> **Status: the generalized model here is design direction; the shipped budget is simpler.**
> AgentConnect `0.1.0` ships a **single global budget**: one amount over one calendar period
> (`set_budget(amount_usd, "daily"|"weekly"|"monthly")`), metered across all real money from
> one `quota_records` ledger, with a fail-closed human gate on every charge (see the
> [README](../README.md)). It does **not** yet support arbitrary intervals, multiple or
> overlapping budgets, arbitrary scopes, or delegation. This document specifies the model the
> plane is built toward so the current implementation is understood as a **starting point, not
> the final model.** Per this repo's honesty rule, the gap between shipped and target is stated
> plainly, not implied.

---

## Why generalize

The current single global daily/weekly/monthly cap is correct for one person, and it must not
be mistaken for the target. A real organization needs many budgets at once — a company cap, a
department allocation, a project ceiling, a per-task limit — that **overlap** and must **all**
be satisfied by a single action. That is a different shape of object, specified below.

## Budget primitives

A budget object must support:

- any customer-defined dollar amount
- any customer-defined time interval
- any number of simultaneous budgets
- overlapping budgets
- arbitrary scopes
- delegated allocations
- nested allocations
- soft limits
- hard limits
- alerts
- reservations
- forecast thresholds
- approval thresholds
- temporary increases
- expiration
- rollover rules
- provider restrictions
- category restrictions
- customer-defined tags

**Intervals are not limited to daily, weekly, or monthly.** Valid examples include `$20/day`,
`$500 every 14 days`, `$10,000/month`, `$100 per task`, `$2,000 per project`, `$50,000/quarter`,
and multiple concurrent windows at once.

## Budget scopes

A budget may apply to any of: individual; organization; business unit; division; department;
team; group; project; workspace; user; agent; task; provider; model; tool; compute class;
hosting provider; marketplace category; or custom tags.

**A single action may need to satisfy several overlapping budgets simultaneously.** The most
constraining applicable cap governs; the interface must make clear *which* cap is the binding
one for a given action (see [the interface](#budget-interface)).

## Delegated autonomy

The point of budgeting is to let organizations set boundaries while teams retain freedom inside
them. Routine purchasing and model selection within an approved allocation should not require
central procurement friction.

```text
Organization budget
    delegates to Engineering

Engineering budget
    delegates to several teams

Teams choose:
    models · tools · hosting · compute · memory services
```

## Enforcement offers alternatives, not just refusal

When an action would exceed a budget, the plane may offer: a local model; a free model; a
cheaper provider; a smaller task; delayed execution; another permitted allocation; an approval
request; or a temporary budget increase. The goal is **efficient resource use, not preventing
useful work.** This generalizes the shipped behavior, where the router already steers toward
free/local as spend nears the single global cap and hard-blocks paid/rented only when it is
exhausted.

## Budget interface

The human-facing view must make immediately clear: what is allocated; what is spent; what is
reserved; what remains; what is forecast; when each budget resets; **which overlapping cap is
constraining an action**; where spending is occurring; and what requires attention.

The interface must serve both ends of the range without forcing either onto the other:

- a **simple personal budget** — one amount, one interval, one meter;
- a **large organization** — many departments and delegated sub-budgets.

The large-organization model must never be forced onto an individual user.

## Customer-owned resources are never charged

Budgets meter real money. Using a customer's own computer, GPU, server, or local model is
**free** and is never assigned a fictional cost — consistent with the ecosystem rule that
Connect never charges for customer-owned resources
([Connect MARKETPLACE_ARCHITECTURE.md](https://github.com/Judgernaut777/Connect/blob/main/MARKETPLACE_ARCHITECTURE.md)).
A budget view may track owned-resource *usage* where useful, but it shows such use as free.

## Relationship to the ecosystem

The Work plane **enforces budgets against work** — it is where spend actually happens. The
Connect control plane provides the *visibility* surface (a unified cost view) without becoming
the billing provider ([Connect DATA_AND_COMPLIANCE_BOUNDARIES.md](https://github.com/Judgernaut777/Connect/blob/main/DATA_AND_COMPLIANCE_BOUNDARIES.md)).
Organization-aware setup configures budgets and delegations; see
[ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md).

## See also

- [README §Spend budget](../README.md) — the shipped single-global budget and the human gate.
- [ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md) — org-aware work and budget behavior.
- [Connect PRODUCT_THESIS.md](https://github.com/Judgernaut777/Connect/blob/main/PRODUCT_THESIS.md) — where budgets sit in the target product.
