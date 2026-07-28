# Organization-aware setup — the Work plane's part

**How AgentConnect participates when Connect is set up for an organization instead of a single
person.** The organizational model itself — onboarding profiles, import/attach/transfer/federate,
resource ownership — is a Connect management-plane concern, defined in
[Connect's `docs/ORGANIZATION_MODEL.md`](https://github.com/Judgernaut777/Connect/blob/main/docs/ORGANIZATION_MODEL.md).
This document says only what the **Work plane** owns inside that model.

> **Status: design direction, not shipped.** AgentConnect `0.1.0` has workspaces, scoped session
> tokens, projects, and the task/artifact/decision/review/handoff ledger — but it has **no
> organization object, no owner field on those resources, and no onboarding flow.** The delegation
> primitives in [docs/HIERARCHICAL_DELEGATION.md](HIERARCHICAL_DELEGATION.md) are the closest
> existing surface. Read this as the target the Work plane converges on, not current runtime; see
> [docs/STATUS.md](docs/STATUS.md) for what is actually true today.

## What the Work plane configures during org-aware setup

The same primitives configure the same way whether one person or a company is onboarding — an
organization just sees more of them (progressive disclosure). The Work-plane surface is:

- **Users and agents** — the principals that do managed work. An org-aware setup binds them to teams,
  departments, or groups defined by the management plane; AgentConnect records *who acted*, it does
  not own the directory.
- **Workspaces and projects** — created per person, per team, or per department. **Workspace
  templates** and **delegated workspace administration** are the org-scale additions: an
  administrator defines a template; a team lead stamps out workspaces from it without central
  involvement.
- **Scoped session tokens** — already the unit of least-privilege access. Under an organization they
  carry the team/department scope so a token cannot reach work outside its boundary.
- **The task/artifact/decision/review/handoff ledger** — the audit surface an organization needs.
  *If it is not recorded in AgentConnect, it did not happen* holds at any scale; org-aware setup adds
  **audit visibility** and **cross-department reporting** as read models over the same ledger, never a
  second source of truth.
- **Delegated administration** — the existing [hierarchical delegation](HIERARCHICAL_DELEGATION.md)
  is how *centralized policy with team-level flexibility* is expressed on the Work plane: a parent
  scope narrows what a child scope may do.

## Ownership and migration

Every workspace, project, and scoped token gains an **owner** (individual, team, department,
organization, or shared group) under the org model. Two rules from the management plane bind here:

- **Joining an organization does not silently transfer workspaces.** A personal workspace stays
  individually owned unless the user explicitly transfers it in the migration preview. *Ownership and
  authorized use are distinct* — a personal machine's workspace can be authorized for approved company
  tasks while remaining personally owned.
- **Stable internal identities are preserved on migration**, so historical task, review, and audit
  records stay attributable after an individual or department joins a larger organization. This is a
  hard requirement on the ledger: a migration must not rewrite the actor of past work.

When a department that already ran its own AgentConnect deployment is imported or federated, its
**task and audit references, workspaces, and delegated scopes are preserved** — the parent applies
broader policy on top rather than rebuilding the ledger.

## Boundary

AgentConnect remains a **compliance and control layer, not a security sandbox**, at organizational
scale too. Org-aware setup adds structure to *who may do what and whether it is recorded*; it does
not turn the Work plane into an isolation boundary it was never built to be.

## See also

- [Connect · `docs/ORGANIZATION_MODEL.md`](https://github.com/Judgernaut777/Connect/blob/main/docs/ORGANIZATION_MODEL.md)
  — the full onboarding model this plane plugs into.
- [docs/HIERARCHICAL_DELEGATION.md](HIERARCHICAL_DELEGATION.md) — the delegation primitives org-scale
  administration builds on.
- [docs/STATUS.md](docs/STATUS.md) — what the Work plane actually ships today.
