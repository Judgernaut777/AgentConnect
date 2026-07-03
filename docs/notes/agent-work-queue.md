# Generic agent work-queue over MCP (ticket-shaped state, human-auditable)

## Context
Prompted by a pattern working well elsewhere: agents are noticeably more reliable
when their durable state and their work backlog live in an **external, queryable,
human-auditable store with ticket semantics** (assignee, status transitions,
dependencies) rather than in-context. Jira/Confluence is one instance of it, but
the transferable part is the *shape*, not the product.

WikiBrain already proves out a minimal version of this: SQLite is the source of
truth, a `research_queue` table is the backlog, and a human gate guards what
becomes truth. Its new `wiki-librarian` pulls pending work and reports back
through fixed doors, never promoting on its own. That's a single-tenant, single-
purpose instance of a general capability that AgentConnect — already the
router + model-manager + runtime — is the natural home for.

## Proposal
Expose a **generic agent work-queue as MCP tools** from AgentConnect, so any
routed agent can claim work, report progress, and hand back results without the
orchestrator holding it all in context.

Sketch of the tool surface:
- `queue_next(pool, capabilities)` → next open ticket matching a pool/skill
- `queue_claim(ticket_id, agent_id)` → lease with a timeout (auto-release on expiry)
- `queue_update(ticket_id, status, note)` → status transitions (open → claimed →
  in_review → done/parked), append-only history
- `queue_report(ticket_id, result)` → structured result behind a review gate
- `queue_add(...)` / `queue_link(a, b, kind)` → dependencies, blocks/blocked-by

Ticket fields worth having from day one: `assignee` = which model/pass (ties into
model-manager routing), `status`, `attempts`/lease, `dependencies`, `origin`, and
a provenance trail. The router already knows which model should handle what —
that's exactly the "assignee" decision, so this composes with existing routing
rather than duplicating it.

## Why AgentConnect specifically
- The router/model-manager already answers "which agent/model handles this" —
  that *is* ticket assignment.
- The runtime's act/tool loop is the consumer: a graph step becomes "claim a
  ticket, do it, report back."
- MCP is already the integration surface, so the queue is reachable by any client
  (including WikiBrain's librarian, which could delegate its `research_queue` to
  this instead of owning it).

## Open questions / non-goals
- Storage: reuse a store AgentConnect already has, or a dedicated SQLite/pg table?
- Leasing/idempotency: how to make double-claim and crashed-worker recovery safe
  (WikiBrain's lesson: filing must refuse already-processed work).
- Human gate: keep report-back as *pending* by default, like the WikiBrain gate,
  vs. auto-accept for trusted pools.
- Explicitly NOT rebuilding Jira — no UI, no workflow builder; just the queue +
  ticket semantics an agent needs.

## Status
Idea capture, not scheduled work. Surfaced now so it's on record before the next
runtime slice is chosen; the WikiBrain `research_queue` + librarian is a working
reference implementation to crib from.
