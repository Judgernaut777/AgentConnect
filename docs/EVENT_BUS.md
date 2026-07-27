# AgentConnect Ecosystem Event Bus

**Status: Part 1 + Part 2 shipped** — canonical store, vocabulary, structural
emission, `GET /events` + `GET /events/stream` (Part 1); the live
observability tree `GET /observe/tree` and the self-contained operator HTML
page `GET /observe` (Part 2, §8).

AgentConnect owns one append-only, sequenced event stream — `event_log`, a
table in the same SQLite ledger every other backplane state lives in — that
BrainConnect, ToolConnect, and ComputeConnect (and any other Connect-family
service) can replay or tail over HTTP. This is **not a new service**: it is a
read surface on the existing ledger, authenticated the same way as every
other AgentConnect route. It carries three producer grades: Engine A's
same-commit `state.changed` skeleton, Engine A's rich advisory events, and
Engine B's advisory bridge (`RouterService`/`WorkQueue` transitions, §3) —
each with its delivery guarantee stated explicitly below.

## 1. Envelope

```json
{
  "seq": 42,
  "event_id": "event_ab12cd34ef56",
  "ts": 1785165727.15,
  "type": "subtask.completed",
  "outcome": "succeeded",
  "actor": "echo_worker",
  "task_id": "task_...",
  "subtask_id": "subtask_...",
  "run_id": null,
  "review_id": null,
  "session_id": null,
  "delegation_id": "deleg_...",
  "parent_delegation_id": "deleg_...",
  "workspace_id": null,
  "entity_id": null,
  "payload": { "summary": "..." }
}
```

Field semantics:

- `seq` — the table's `AUTOINCREMENT` primary key. Durable, cross-process,
  monotonic, **never reused**, and survives a process restart (the same
  SQLite sequence counter). This is the wire ordering key — not the
  in-process `AgentObservationEvent.sequence` the emitter also carries, which
  is unrelated and stays internal.
- `ts` — wall-clock write time (`time.time()`), advisory only; `seq` is the
  ordering authority.
- `type` — a stable wire string from the vocabulary below.
- `outcome` — `succeeded | failed | cancelled | denied | timed_out | unknown |
  null`. `null` for events with no terminal-outcome concept (`task.created`,
  `state.changed` transitions between non-terminal states, …).
- `actor` — the agent/system id that caused the event. Never a display name a
  caller can spoof — it is the token's/worker's own id.
- Correlation ids — `task_id`/`subtask_id`/`run_id`/`review_id`/`session_id`/
  `delegation_id`/`parent_delegation_id`/`workspace_id`: whichever apply.
  Nothing is inferred from timestamps; the agent tree is reconstructable from
  these ids alone.
- `entity_id` — the raw entity id a `state.changed` transition applied to
  (redundant with one of the correlation ids above for most vocabularies;
  present so `execution_state` transitions, which have no `event_log` FK
  column of their own, are still traceable).
- `payload` — privacy-safe, bounded (see §7). Never a prompt, transcript, or
  free-form content field.

## 2. Seq contract

- **Monotonic, not necessarily dense.** A refused/noop transition and an
  `INSERT OR IGNORE` duplicate both consume no `seq` at all — gaps are
  normal and carry no meaning.
- **`since` is EXCLUSIVE** (`seq > since`). A consumer resumes with the last
  `seq` it actually saw. Never do arithmetic on `seq` (`since + 1`, batch
  size math) — resume with the literal value.
- **Restart-safe.** `seq` is `AUTOINCREMENT`, which SQLite tracks in
  `sqlite_sequence` and never reuses even across a close/reopen of the same
  database file.
- **Idempotent.** `event_id` is `UNIQUE`; a second insert with the same id is
  silently ignored (`INSERT OR IGNORE`), so a replayed emission (an at-most-
  once-loss Path 2 retry, a mirrored write) never double-counts.

## 3. Delivery guarantees per producer path

Three producers write into the same `event_log` table, with **disjoint
coverage by construction** — no dedup problem, because nothing ever emits the
same logical event through two paths: Path 1 and Path 2 cover Engine A with
disjoint event types, and the Engine B bridge covers a different engine on a
different database entirely (its `state.changed` rows are distinguishable by
the `engine: "b"` payload marker and their `task_state`/`ticket_status`
vocabularies, which Path 1 never writes).

### Path 1 — `state.changed` (structural, same-commit, guaranteed)

`SqliteStorage._insert_transition_audit` — already invoked inside
`transition_row`'s single locked commit for **every applied transition** on
all 7 Engine-A state-carrying tables (`tasks`, `subtasks`, `worker_runs`,
`reviews`, `approvals`, `manager_sessions`, `executions`) — additionally
inserts one `event_log` row of type `state.changed`, on the SAME connection,
before the SAME commit. This holds:

- for every call site today, including reaper/reconcile/cascade/cancel paths,
  with **zero code changes** at any of them (the emission point is the writer
  `TransitionAuthority` already mandates);
- for any future call site that forgets to call `_observe(...)` — there is no
  way to apply a transition without this row;
- with **no observability provider configured at all** — Path 1 does not go
  through the emitter.

Only `outcome == "applied"` transitions produce a `state.changed` row.
Refused/noop outcomes are still recorded in the legacy `events` audit table
(unrelated to this bus) but never in `event_log`.

An Engine-A `state.changed` payload is always exactly `{vocabulary, src,
dst, reason}` — enum strings and a truncated (300-char) reason, nothing
else. It never carries a title, a prompt, or any free-text field a caller
supplied. (Engine-B bridge `state.changed` payloads are `{vocabulary, src,
dst, engine}` — see "Engine B bridge" below.)

### Path 2 — rich events (ordered-after, advisory, at-most-once-loss-on-crash)

Every existing `AgentConnectService._observe(...)` call site (~35 of them —
`task.created`, `subtask.created`, `worker.started`, `tool.authorized`, …)
already builds a normalized `AgentObservationEvent` and fans it out through
`ObservabilityEmitter` -> `CompositeObservabilityProvider` -> each configured
provider. The always-on `SqliteEventLogProvider` (`passive=True`, no live
surface) is now unconditionally one of those providers — registered by
`AgentConnectService.__init__`'s default emitter and re-added by
`bind_observability` if a caller's own composite does not already carry one
by name — so `observability.enabled` is `True` in every deployment, even one
with zero configured providers, and every rich event becomes a durable
`event_log` row with no extra infrastructure.

**Gap story:** a process crash between the ledger commit that triggered the
lifecycle change and the `_observe(...)` call that persists its rich sibling
loses that one row. The same-commit `state.changed` skeleton (Path 1) never
does — a replayer can always reconstruct the full state history from
`state.changed` alone, and detect a missing rich sibling by its absence (no
corresponding `task.created`/`subtask.completed`/… at the expected
correlation ids).

Because both paths write the same SQLite database under `storage._lock`,
`seq` order == commit order: a transition's `state.changed` row always
sequences **before** its rich sibling (Path 1 writes same-commit as the
state write; Path 2 writes strictly after).

### Engine B bridge (advisory, opt-in binding)

Engine B — `RouterService`'s task pipeline and the federated `WorkQueue`'s
ticket lifecycle — runs against `SharedMemory`, a **separate SQLite
connection/database** from the core ledger. Same-commit emission into
`event_log` is therefore architecturally impossible for it; without a bridge
its transitions were entirely invisible to the bus.

The bridge: `SharedMemory.bind_event_bus(sink)` accepts a sink with
`SqliteStorage.append_bus_event`'s keyword signature. Once bound:

- every **applied** Engine-B task-pipeline transition
  (`SharedMemory.transition_task`, the writer Engine B's one
  `TransitionAuthority` mandates) mirrors one `state.changed` event with
  payload `{vocabulary: "task_state", src, dst, engine: "b"}`, emitted
  strictly AFTER its own commit;
- every **applied** work-queue ticket lifecycle write (claim, report,
  approve/reject, cancel, lease-expiry requeue, park — the single
  `WorkQueue._ticket_audit` funnel) mirrors one `state.changed` event with
  payload `{vocabulary: "ticket_status", src, dst, engine: "b"}`.

Semantics are **bridge-grade, weaker than Path 1**: advisory (a sink failure
never breaks Engine B), ordered-after (never same-commit), and — for ticket
events, which are audited before their caller's commit — a subsequent
rollback can leave one stray advisory event. Engine B's own `logs` table
remains authoritative. Free-text `reason` fields are deliberately **omitted**
from bridge payloads: no privacy tier is in scope at that layer to gate them
(§6), and the local `logs` rows keep the full detail. `task_id` on a bridge
event is an **Engine-B task id** (a different namespace from Engine-A task
ids) — the `engine: "b"` payload marker is how consumers tell the streams
apart.

Wiring: `create_app` binds the bridge automatically whenever the API
deployment holds both the core ledger and a router
(`router.memory.bind_event_bus(service.storage.append_bus_event)`). A
standalone Engine-B deployment can point the bridge at any `SqliteStorage`'s
`append_bus_event`. Unbound (the default for bare `SharedMemory`/`WorkQueue`
construction), the bridge is a no-op. Engine-B row **creations** (a new task
row, a new ticket) do not bridge — the bus picks the entity up at its first
transition; creation facts live in Engine B's own store.

## 4. Vocabulary

`agentconnect.core.observability.model.EventType` is the ONE enum — no
parallel vocabulary exists. Every wire `type` string is a `.value` of a
member of this enum. New members added for the event bus:

| member | wire id | producer |
|---|---|---|
| `state_changed` | `state.changed` | Path 1, every applied transition |
| `subtask_completed` | `subtask.completed` | `_record_result` success branch, beside `worker.completed` |
| `subtask_failed` | `subtask.failed` | `_record_result` failure branch + `_cascade_dependency_failure` |
| `memory_promoted` | `memory.promoted` | `promote_memory_candidate`, after a successful promotion |
| `provider_offline` | `provider.offline` | `_note_component_health`, edge-triggered |
| `provider_degraded` | `provider.degraded` | `_note_component_health`, edge-triggered |
| `provider_recovered` | `provider.recovered` | `_note_component_health`, edge-triggered |
| `tool_executed` | `tool.executed` | **reserved** — no producer yet; see §4.2 |

### 4.1 Mapping from this document's originating GOAL vocabulary

| GOAL name | wire type(s) |
|---|---|
| TaskCreated | `task.created` |
| TaskClassified | `subtask.routed` + `compute.placed` |
| TaskClaimed | `task.claimed` |
| WorkerStarted | `worker.started` |
| SubtaskSubmitted | `subtask.created` |
| SubtaskCompleted | `subtask.completed` |
| ToolAuthorized / ToolDenied | `tool.authorized`, distinguished by `outcome` (`null`/absent = allow, `denied` = deny) — see §4.2 |
| ToolExecuted | `tool.executed` (reserved, §4.2) |
| ArtifactUploaded | `artifact.created` |
| MemoryCaptured | `memory.captured` |
| MemoryPromoted | `memory.promoted` |
| ProviderOffline / ProviderDegraded | `provider.offline` / `provider.degraded` |
| TaskCancelled / TaskCompleted / TaskFailed | `task.cancelled` / `task.completed` / `task.failed` |

### 4.2 Two deliberate vocabulary decisions

- **`ToolDenied` is not a separate member.** `tool.authorized`'s `outcome`
  distinguishes an allow from a deny (a policy deny or an `unavailable`
  fail-closed outage deny) — this was already the enum's documented design
  before the event bus existed; the bus simply persists it durably. Filter
  with `?type=tool.authorized&outcome=denied`.
- **`tool.executed` is reserved, not wired.** No code path in this
  repository today observes an actual tool *invocation* inside a bounded
  worker's own in-process loop (only the pre-spawn declared-tool-set
  authorization, `tool.authorized`, and — for AgentConnect's own runtime —
  the per-call redeem boundary ADR 0009 describes). The wire id is fixed now
  so a future sandbox-runtime worker-report integration does not have to
  invent one later.

### 4.3 Provider health edge-triggering

No poller exists in this codebase — health is checked wherever it is already
computed (`AgentConnectService.readiness()`, once per memory backend). A
repeated check with an unchanged classification emits nothing: a component
that stays down (or stays up) does not spam the bus. The very first
observation of a component establishes a silent baseline if it is already
healthy, but fires immediately if the first-ever check already finds it down
— an outage that predates this process is real news. `"disabled"`/`"unknown"`
status strings are not meaningful health signals and are ignored outright.

## 5. HTTP surface

Both routes are declared in `agentconnect-api`'s `ROUTE_ACTIONS` under the
action `list_events`, which is operator-plane only
(`agentconnect.core.sessions.OPERATOR_ACTIONS`) — the same posture as
`list_sessions`/`audit_task`: this is fleet-wide ledger visibility, not
bound to one manager/reviewer token's task scope. Auth is the ordinary
`Authorization: Bearer <operator-token>` header every other route uses.

### `GET /events?since=0&limit=100&type=a,b&task_id=&outcome=`

```json
{ "events": [ /* envelope, §1 */ ], "latest_seq": 128 }
```

- `since` (int, default 0) — exclusive lower bound.
- `limit` (int, default 100) — clamped server-side to `[1, 500]`.
- `type` (comma-separated) — an unknown type name is a `400 invalid_request`,
  never a silently-empty filter.
- `task_id`, `outcome` — exact-match filters.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8790/events?since=0&limit=50&type=task.created,subtask.completed"
```

### `GET /events/stream` (Server-Sent Events)

The live tail. `since` (query) takes priority over a `Last-Event-ID` resume
header, which takes priority over "start from the current tail" (a fresh
live-only subscriber gets `latest_bus_seq()` as its starting cursor, not
`0`). Each frame:

```
id: 42
event: subtask.completed
data: { ...envelope... }

```

An idle stream emits an SSE comment (`: keepalive\n\n`) roughly every 15
poll cycles so a proxy/load-balancer does not time out the connection.

Because an `Authorization` header cannot be set on a browser `EventSource`,
this route is for **programmatic consumers only**:

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8790/events/stream?since=0"
```

The browser-facing operator page (`GET /observe`, §8) fetch-polls `GET
/events` instead — it never uses `EventSource` against this route.

## 6. Privacy

- `state.changed` payloads structurally contain only `{vocabulary, src, dst,
  reason}` — enum strings and a truncated reason, never anything a caller
  supplied as free text.
- Every other event's `metadata` passes through three layers before it can
  reach `event_log`: (1) the caller-side truncated summaries the existing
  emission sites already used (`summary[:120]`, `reason[:160]`, …), (2) the
  emitter's own `_redact_metadata` (masks credential-shaped keys, scans every
  remaining string through the safety redactor), (3) the
  `SqliteEventLogProvider`'s own `_scrub` (drops `instructions`/`prompt`/
  `transcript`/`output`/`output_text`/`goal` keys outright, masks
  credential-shaped keys again as defense-in-depth, bounds every string to
  500 chars).
- **Every free-text field a subtask-lifecycle event carries is tier-gated at
  WRITE time**, at the same `PRIVACY_STRICTNESS >= 3`
  (`local_only`/`secret_sensitive`) cutoff, replaced by the literal string
  `"[redacted]"` — nothing to redact later because nothing was ever stored.
  The gated fields:
  - `subtask.created`'s `title` (`_subtask_event_meta`) — an operator-chosen
    string that can hold content;
  - `worker.completed`/`worker.failed`/`subtask.completed`/`subtask.failed`'s
    `summary` (`_tier_gated_free_text`) — worker-supplied free text; a worker
    that echoes its input back (the bundled `EchoWorker` does exactly this)
    would otherwise carry a strict-tier subtask's own title into the durable
    log;
  - `subtask.denied`'s `reason`, on BOTH deny paths — the operator's
    `deny_subtask` explanation (which often restates why the work is
    sensitive) and the tool-governor's policy reason. The governor deny's
    tool identifiers/flags ride ungated (a consumer still learns which tool),
    but `as_metadata()`'s own free-text `reason` is replaced by the gated
    value;
  - `tool.authorized`'s `reason` (the governor's prose), gated against the
    subtask named on the event; with no subtask in scope (a generic
    `authorize_tool_use` probe) there is no tier to gate against and the text
    rides bounded to 200 chars.
  An unknown/corrupted tier fails CLOSED to `"[redacted]"`.
- **`task.created`'s `title` is bounded (120 chars) and secret-scanned but
  NOT tier-gated** — a task carries no privacy tier of its own (tiers live on
  subtasks, which do not exist yet at creation time), and the append-only log
  cannot be retro-redacted when a strict-tier subtask arrives later.
  `/observe/tree` DOES withhold the same title at read time once the task's
  effective tier turns strict; the creation-time bus event cannot follow.
  Operational rule: sensitive content belongs in subtask
  title/instructions (tier-gated at write time), never in a task title.
- **Engine B bridge events carry no free text at all** — `{vocabulary, src,
  dst, engine}` only; `reason` is omitted because no privacy tier is in scope
  at that layer to gate it (§3, Engine B bridge).

## 7. Consumer guidance

- **BrainConnect** already emits into `AgentObservabilityProvider` (Lane 8);
  it can now also read the durable ledger back via `GET /events` for
  replay/audit, using the same correlation ids its own event model carries.
- **ToolConnect** should correlate `tool.authorized` payload `decision_id`
  with its own `AssertionRecord`s — the event bus does not duplicate
  ToolConnect's own audit trail, it just makes the *decision* (allow/deny +
  which tool + which task/subtask/run) visible on AgentConnect's side too.
- **ComputeConnect** should watch `compute.placed` (a subtask's routing
  decision landed on a specific location/provider) and `provider.*` (a
  configured dependency's health changed).

## 8. Live observability tree (Part 2)

`GET /observe/tree?task_id=&include_terminal=0` returns the live manager ->
worker -> subagent hierarchy, assembled at READ time from the same ledger
`/events` replays — no new tables, no caching, no live process
introspection. Operator-plane, same posture as `/events` (`observe_tree` in
`OPERATOR_ACTIONS`, `sessions.py`).

```json
{
  "generated_at": 1785166555.80,
  "latest_seq": 42,
  "roots": [ { "kind": "task", "id": "task_...", "...": "...", "children": [ ] } ]
}
```

Default scope is every **open** task (`include_terminal=0`); a task-scoped
call (`?task_id=...`) always returns that task whether it is terminal or
not — a 404 only means the id does not exist at all.

### Node shape

```json
{
  "kind": "task | session | subtask | run | review",
  "id": "...", "title": "...", "state": "...",
  "privacy_tier": "public | public_redacted | repo_sensitive | local_only | secret_sensitive",
  "prompt": "... | null",
  "model": "... | null",
  "current_tool": "... | null",
  "tokens": { "input": 12, "output": 34 },
  "cost_usd": 0.0,
  "lease": { "holder": "...", "expires_at": 123.0, "fence": null },
  "artifacts": [ { "id": "...", "name": "...", "created_at": 123.0 } ],
  "elapsed_s": 1.2, "started_at": 123.0, "actor": "...",
  "delegation_id": "...", "parent_delegation_id": "...",
  "children": [ ]
}
```

Assembly, per task root: `manager_sessions` attach to the task (or to their
own parent delegation, if nested); `subtasks` attach the same way, each with
its `worker_runs` as children; `reviews` attach the same way. `artifacts`
attach to the subtask node their `metadata.subtask_id` names, else to the
task root. This mirrors `AgentConnectService.agent_tree` (a narrower,
pre-existing surface this module does not modify), enriched with prompt,
tool, model, tokens, cost, lease, and elapsed time.

Freshness caveats, stated rather than hidden:

- **`tokens`/`cost_usd` are `null` until a run reports them** — they land
  only at `worker_runs.metrics_json` write time (on completion), never
  fabricated as `0` while a run is still in flight. Values are read from
  whichever key names a worker actually used (`tokens_in`/`tokens_out`/
  `estimated_cost_usd`, or `input_tokens`/`output_tokens`/`cost_usd`,
  including nested under a `usage` dict).
- **`current_tool` is "the last `tool.authorized` event for this subtask"**,
  a ledger fact, never a claim about what is executing right now — no live
  invocation tracking exists, and none is invented. Sourced from one
  `event_log` query per task (`type='tool.authorized'`), not one query per
  subtask.
- **`lease`** comes from the `claims` table for a task/session node (a
  primary-manager claim), with `fence: null` — task claims carry no fencing
  token. A subtask node's `lease` is `None` in this deployment: the
  federated `WorkQueue`'s ticket lease/fence concept lives in a separate
  service (`RouterService`) with no `subtask_id` handle back into
  `AgentConnectService` — an honest gap, not a guessed value. Wiring that
  overlay is future work.
- **`elapsed_s`** freezes at the entity's own terminal timestamp
  (`updated_at`/`ended_at`/`finished_at`) once terminal, otherwise ticks
  against "now".

### Privacy (fail-closed, enforced at read time)

Redaction happens in the tree builder when a node is serialized — storage is
never mutated. `prompt` (a subtask's `instructions`, or a session's
launch/shell command) and `title` (once strictness reaches
`local_only`/`secret_sensitive`, the same `PRIVACY_STRICTNESS >= 3`
threshold `_subtask_event_meta` already applies at event-bus write time) are
governed by the subtask's own `privacy_tier` (a session/review/task node
uses the task's *effective* tier — the strictest tier among its subtasks):

| tier | `prompt` in `/observe/tree` |
|---|---|
| `public` | redactor-scanned, truncated to 2000 chars |
| `public_redacted`, `repo_sensitive` | redactor-scanned, truncated to 400 chars + `"…[truncated]"` |
| `local_only` | constant `"[withheld: local_only]"` — no length or content signal |
| `secret_sensitive` | constant `"[redacted: secret_sensitive]"` — never serialized, no length or content signal |
| unparseable/unknown tier string | treated as `secret_sensitive` (fail-closed) |

### The operator page

`GET /observe` serves a single self-contained HTML page — inline CSS/JS
only, zero external assets (no CDN script, no web font, no remote image),
dark-mode-aware via `prefers-color-scheme` and a `data-theme` override. It
is **public** (`authz.PUBLIC_ROUTES`): the page itself is a constant string
naming no task and returning no ledger content; every data call its JS
makes (`GET /observe/tree`, `GET /events?since=...`) carries the operator's
own bearer token (typed into the page, persisted to `localStorage`) to the
real, protected routes. Same precedent as the router's `queue_web` operator
dashboard.

The page renders the tree as nested, natively clickable `<details>/<summary>`
elements (no framework) and polls `GET /events?since=<lastSeq>&limit=200`
every ~2s (backing off to 10s on repeated auth/network failure). Any polled
event whose type starts with `task.`, `subtask.`, `worker.`, `session.`,
`review.`, or `state.` triggers a `GET /observe/tree` refetch — the
same-commit `state.changed` skeleton (§0/§4) guarantees this fires even when
a rich sibling event was lost to a crash. Clicking a node shows its full
detail (prompt, tool, model, tokens, cost, lease, artifacts, elapsed,
delegation ids) in a side panel. It intentionally fetch-polls rather than
using `EventSource` (see §5) — a browser `EventSource` cannot set an
`Authorization` header.
