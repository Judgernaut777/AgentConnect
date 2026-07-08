# Hierarchical Delegation (Track 4)

> **Status:** implemented. A planner/manager worker may decompose a task into
> sub-tasks; the router runs each as a child agentic sub-run at the next depth and
> folds the child summaries back into one parent summary. Off by default.

A single agentic worker holds one task's whole context in its window. That does not
scale: a large migration, audit, or refactor has more moving parts than one context
can carry. Track 4 lets a worker **decompose** — emit self-contained sub-tasks — and
have the router execute and re-synthesize them, so each node keeps the *smallest*
context it needs. This is **recursive context virtualization**: the same principle the
router already applies (large output → shared memory → compact summary + refs), now
applied to the work itself.

The design center is **one router, no nested control planes.** A child is a *sub-run*
of its parent task — not a new first-class queued task. The parent's reservation,
evaluation, and state machine cover the whole tree; the router schedules and
synthesizes; the workers only ever *record* sub-tasks and *finish*.

## The two halves

### 1. Runtime — the `delegate` action

A worker requests decomposition with one action (see [AGENT_RUNTIME.md](AGENT_RUNTIME.md)):

```json
{"action": "delegate", "task": "<a self-contained sub-task>", "agent_type": "<optional>"}
```

It is recorded on `WorkerResult.subtasks` (a list of `SubTask{task, agent_type?,
privacy_class?}`). The worker **never blocks** on a delegated child — it keeps its own
context small and finishes; the router hands it nothing back inline (the synthesized
summary is what the *manager* sees). The capability mirrors the write-only `remember`
seam and is bounded at the runtime level:

- advertised in the prompt **only** while `delegation_depth < max_delegation_depth`, so
  a leaf worker is never told to decompose work it must do itself;
- gated in the graph on `allow_delegation`, the depth limit, **and** a per-run
  `max_subtasks` fan-out cap.

### 2. Router — decompose → execute → synthesize

When an agentic worker completes with a non-empty `subtasks` list *and* delegation is
enabled, `RouterService._agentic_tree` (in `service.py`) recurses:

1. **Execute** each sub-task as its own agentic sub-run at `depth + 1`, on the *same*
   provider the parent is already running on. Child task ids are namespaced under the
   parent (`‹parent›/d1.1`, `/d1.2`, …), and each child's raw `WorkerResult` is stored
   as a `child_output` artifact under the parent task — so the whole tree is inspectable
   from the root.
2. **Synthesize** — one gateway generation folds the parent's own findings plus every
   child summary into a *single* consolidated parent summary. Confidence collapses to
   the **weakest link** (`min` across the tree); risks and changed artifacts **union**.

Token usage for the *entire tree* (every worker step + the synthesis call) accumulates
into one meter and is reconciled **once** against the parent's reservation, exactly like
a flat agentic run. The metering happens in a `finally`, so a mid-tree failure still
leaves partial usage to bill.

## Enabling it

Off by default — an agentic run stays a single worker unless you opt in:

```python
svc = RouterService.create(local_client=...)
svc.enable_delegation = True     # advertise + honor the delegate action
svc.max_delegation_depth = 2     # tree depth cap (default 2)
svc.max_subtasks = 8             # per-node fan-out cap (default 8)
```

No task-submission surface changes: a manager still calls the vanilla
`submit_task(execution="agentic")` MCP tool. Enabling delegation only makes the action
*available*; a task that doesn't decompose simply runs flat.

## Privacy: monotonic, never laundered down

A child's `privacy_class` is a **proposal**. The router clamps it with the same
child ⊆ parent monotonicity the [work queue](WORK_QUEUE.md) enforces on dependency
edges (`_child_privacy_class`):

- **No proposal** → the child **inherits** the parent's class.
- A proposal is honored **only** if it is *equal-or-more-restrictive* — its
  admissible-tier set (from the single source of truth, `common/privacy.py::allowed_tiers`)
  is a **subset** of the parent's. A stricter-but-runnable class (e.g. `repo_sensitive`
  under a `public` parent) is honored.
- A **looser** proposal (e.g. `public` under a `repo_sensitive` parent) is **clamped up**
  to the parent — sensitive work can never be laundered down a delegation edge to a
  looser tier.
- **Fail-closed:** an *unknown* class (absent from the routing map) is clamped, never
  honored. A *known* class with an empty tier set — `secret_sensitive`, the strictest of
  all — is a valid stricter proposal, so it is distinguished from "unknown" by map
  membership.
- A child that resolves to `secret_sensitive` is **refused, not run**: like the top-level
  dispatch guard, `secret_sensitive` content must never reach an LLM. It is folded in as a
  failed child (risk `secret_sensitive_child_refused`) and the parent still completes.

## Bounds (why recursion can't run away)

| Guard | Where | Effect |
|-------|-------|--------|
| `delegation_depth < max_delegation_depth` | prompt + graph + router | leaves don't decompose; grandchildren stop at the limit |
| `max_subtasks` | graph | caps fan-out per node |
| privacy clamp | router | child ⊆ parent; no downgrade |
| single reservation / meter | router | whole tree billed once, partial usage survives failure |

Total nodes are bounded by `max_subtasks ^ max_delegation_depth`. Defaults (8, 2) keep it
modest; raise deliberately.
