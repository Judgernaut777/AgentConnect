# AgentConnect Handoff — Final Core Stack: Temporal + WikiBrain + Cognee + Graphiti

> Authored by the project owner, 2026-07-10. Fourth handoff. Supersedes the memory half of
> `docs/BACKPLANE_SPEC_ADAPTERS.md` (Part A) — the `MemoryAdapter` interface survives; what changes
> is that there are now *three* backends with *different trust roles*, and a `ContextBuilder` in
> front of them. See `docs/BACKPLANE.md` for as-built status.

## Goal

A layered architecture where task/workflow state, memory, retrieval, and human workflow are
separated. Core stack: **AgentConnect · Temporal · Linear · WikiBrain · Cognee · Graphiti**.

Soft user-preference memory is **not** in the first core implementation. Hard preferences and
policy constraints live in AgentConnect config or WikiBrain scoped claims.

## 1. Final architecture

```
Human → Linear → AgentConnect API / MCP / CLI → AgentConnectService
                                                  ├── AgentConnect DB / artifact store
                                                  ├── Temporal workflows
                                                  ├── WikiBrain trusted memory
                                                  ├── Cognee broad retrieval
                                                  └── Graphiti temporal graph
```

Context flow:
```
Manager / Subagent → AgentConnect ContextBuilder
                       ├── AgentConnect DB / artifacts
                       ├── WikiBrain trusted claims
                       ├── Cognee broad project retrieval
                       └── Graphiti temporal relationship graph
                     → bounded context pack → Manager / Subagent
```

Execution flow: manager submits subtask → AgentConnect creates subtask → Temporal starts workflow
→ activities: recall context, route subtask, run worker, request approval if needed, save
artifact, update Linear, capture memory candidate.

## 2. Responsibilities by layer

**AgentConnect** owns: tasks, subtasks, manager claims, review tickets, decisions, attempts,
artifacts, route history, worker runs, approval records, handoff summaries, **memory routing**,
**context injection**.
It does **not** own: human issue planning UI, workflow durability internals, knowledge-graph
storage internals, broad document/RAG indexing internals, local model runtime management, soft
conversational preference memory.

**Temporal** owns: workflow execution, activity retries, durable timers, approval waits, signals,
queries, updates, crash recovery, long-running subtask orchestration. It does not own canonical
task state or long-term knowledge. Workflow history may *produce* memory candidates, but managers
must not query Temporal as a memory system.

**Linear** owns: human-visible issue state, comments, labels, assignments, approvals, status
visibility. Not the canonical database.

**WikiBrain** is the **trusted memory authority**. It owns pending candidates, promoted trusted
claims, rejected candidates, source/provenance links, scopes, supersession, contradiction records,
recall feedback, Obsidian projection. It answers: *What do we trust? Where did it come from? Who
promoted it? What scope? Is it current? Was it superseded? Should this be shown?*
It does not own task state, Temporal state, Linear state, worker execution, model routing, or raw
artifact storage.

**Cognee** is the **broad retrieval layer**: broad project/document recall, semantic retrieval,
graph/RAG retrieval over notes, docs, artifacts, issue history, selected sources. **Cognee is not
the trusted authority by itself.**

**Graphiti** is the **temporal relationship graph**: time-aware relationships, entity/relation
evolution, supersession relationships, model/worker performance over time, project evolution,
"what changed?" retrieval. It indexes promoted claims, selected AgentConnect events,
decision/supersession relations, worker/model performance facts, selected source metadata. It must
not be a dump of raw logs, chat transcripts, or unreviewed worker output.

## 3. User preferences policy

**Leave out initially:** tone/formatting preferences, soft conversational habits, general
assistant personalization, temporary interaction preferences. ("User likes tables.", "User prefers
concise answers.")

**Include hard preferences and policies** — anything affecting routing, privacy, cost, or safety.
("Repo-sensitive code must stay local.", "Paid cloud execution requires approval.", "Do not send
secrets to external workers.") These live in AgentConnect config or WikiBrain scoped claims and are
on the core policy path.

## 4. Memory write path

**Bad:** Claude writes directly to Cognee. Codex writes directly to Graphiti. Workers write to
WikiBrain as promoted facts. AgentConnect queries all systems later.

**Good:**
```
Manager/worker result → AgentConnect event → WikiBrain pending candidate
  → human/librarian promotion → promoted WikiBrain claim
      ├── indexed into Cognee for broad retrieval
      └── indexed into Graphiti for temporal graph retrieval
```

Capture rule: agents and workers may create pending candidates; **they may not promote trusted
memory by default.** `capture_memory_candidate → pending`; `promote_memory_candidate →
human/librarian only`.

## 5. Memory read path

Managers and subagents must not query Cognee, Graphiti, or WikiBrain directly for task context.
They ask AgentConnect for a context pack.

The pack must be: bounded, role-specific, source-labeled, scope-filtered, deduplicated,
supersession-aware, and explicitly marked by trust status. Default size: **5–10 memory items.**

## 6. ContextBuilder

```python
class ContextBuilder:
    def build_context_pack(
        self, task_id: str, profile: str, query: str | None = None, max_items: int = 8,
    ) -> ContextPack: ...
```

Steps: load task state → determine profile → select backends → query WikiBrain/Cognee/Graphiti →
trust filters → scope filters → remove superseded → deduplicate → rank by authority, relevance,
recency, role fit → return one bounded pack.

## 7. MemoryRouter

```python
class MemoryRouter:
    def select_backends(self, profile: str, task_id: str, query: str | None = None) -> list[str]: ...
```

| Profile | Backends |
|---|---|
| `manager_brief` | AgentConnect DB, WikiBrain, Cognee |
| `worker_brief` | AgentConnect artifacts, WikiBrain, optional Cognee |
| `reviewer_brief` | AgentConnect review/artifacts, WikiBrain |
| `implementation_constraints` | WikiBrain only |
| `known_failures` | WikiBrain, Graphiti, AgentConnect run history |
| `model_performance` | AgentConnect worker runs, WikiBrain, Graphiti |
| `project_evolution` | WikiBrain, Graphiti |
| `broad_project_rag` | Cognee |
| `hard_policy` | AgentConnect config, WikiBrain |

## 8. MemoryRanker

```python
class MemoryRanker:
    def merge_and_rank(self, packs: list[RecallPack], profile: str, max_items: int) -> RecallPack: ...
```

Handles duplicate facts, conflicting facts, superseded facts, pending facts, backend disagreement,
source priority, scope priority, trust status, recency, profile relevance, context budget.

**Default authority order:**
1. AgentConnect locked decisions and hard config
2. WikiBrain promoted *verified* claims
3. WikiBrain promoted high-confidence claims
4. Graphiti temporal relationships tied to promoted claims
5. Cognee broad retrieval results with source links
6. Pending or unknown-status memory, only if explicitly requested

**Pending memory must not be injected by default.**

## 9. Core memory interfaces

Retain the generic `MemoryAdapter` (`backend_name`, `recall`, `capture_candidate`,
`record_feedback`, `health`). Implement: `WikiBrainMemoryAdapter`, `CogneeMemoryAdapter`,
`GraphitiMemoryAdapter`, `NoopMemoryAdapter`, `StaticMemoryAdapter` (tests).

## 10. Context profiles

* **manager_brief** — enough to plan, continue, or hand off. Task state, locked decisions, recent
  attempts, important artifacts, WikiBrain trusted claims, Cognee broad related context, Graphiti
  change/supersession warnings if relevant.
* **worker_brief** — only what the bounded subtask needs: instructions, allowed artifacts, hard
  constraints, WikiBrain implementation constraints, small Cognee recall only if needed. **Avoid**
  broad task history, manager debate, soft preferences, untrusted memory.
* **reviewer_brief** — review ticket, artifact under review, test logs, locked decisions, WikiBrain
  implementation constraints, known failures.
* **implementation_constraints** — hard constraints and locked/promoted decisions only.
* **model_performance** — AgentConnect `worker_runs`, WikiBrain promoted model-performance claims,
  Graphiti temporal model/task/outcome relationships.
* **project_evolution** — WikiBrain supersession records, Graphiti temporal graph, AgentConnect
  decision history.

## 11. Temporal integration

Workflow code must not call memory backends directly. Use activities:

```
recall_context_activity(task_id, profile, query, max_items) -> ContextPack
capture_memory_candidate_activity(task_id, text, source_ref, origin_actor) -> CaptureResult
index_promoted_memory_activity(claim_id) -> None
record_memory_feedback_activity(...) -> None
```

Example `SubtaskWorkflow`: load subtask → `recall_context_activity(profile="worker_brief")` →
`route_subtask_activity` → `run_worker_activity` → `save_artifact_activity` →
`capture_memory_candidate_activity` if a useful reusable fact exists → `update_linear_activity`.

**Memory failures must not crash workflows by default:** continue without external memory, record
a warning, include the warning in task context/status.

## 12. Indexing policy

**WikiBrain → Cognee.** Index: promoted claims, source summaries, selected artifacts, project
docs, decision summaries, review summaries. Do **not** index as trusted: raw worker logs,
unreviewed subagent output, pending candidates, secret-sensitive artifacts.

**WikiBrain → Graphiti.** Index: promoted claims, claim-source relationships, supersession links,
contradiction links, model/worker performance facts, project evolution facts, Temporal-derived
lifecycle summaries after review. Avoid: untrusted raw chat, all workflow events, all logs, all
pending candidates.

## 13. Linear integration

Memory-related Linear updates must be compact:

```
Memory candidate captured: "Potential reusable lesson captured for review."
Memory promoted: "Promoted memory: local-qwen is acceptable for read-only search but not
                  preferred for auth patch review."
Memory conflict: "Potential contradiction detected between promoted claims claim_012 and claim_019."
```

Do not post full memory backend dumps into Linear.

## 14. Configuration

```yaml
memory:
  enabled: true
  trusted_authority: wikibrain
  backends:
    wikibrain: {enabled: true, base_url: http://localhost:8787}
    cognee:    {enabled: true, base_url: http://localhost:8001}
    graphiti:  {enabled: true, base_url: http://localhost:8002}
  defaults:
    trusted_only: true
    include_pending: false
    include_superseded: false
    max_items: 8
  profiles:
    manager_brief:              {backends: [wikibrain, cognee, graphiti], max_items: 8}
    worker_brief:               {backends: [wikibrain, cognee],           max_items: 5}
    reviewer_brief:             {backends: [wikibrain, graphiti],         max_items: 8}
    implementation_constraints: {backends: [wikibrain],                   max_items: 6}
    model_performance:          {backends: [wikibrain, graphiti],         max_items: 8}
```

## 15. Do not include soft preferences yet

No Mem0/Supermemory in the core stack. If needed later, add a `SoftPreferenceMemoryAdapter` — but
the first version must not mix soft conversational memory with project facts. Hard preferences are
AgentConnect config or WikiBrain user/project/repo-scoped promoted claims.

## 16. Tests

AgentConnect runs with memory disabled · ContextBuilder returns task-only context if memory
disabled · WikiBrain promoted claim appears in `manager_brief` · WikiBrain pending claim excluded
by default · Cognee result appears as broad retrieval, not trusted authority · Graphiti result
appears as temporal relationship, not trusted authority · superseded WikiBrain claim excluded by
default · Graphiti supersession warning appears in `project_evolution` · MemoryRanker deduplicates
same fact from WikiBrain and Cognee · MemoryRanker prioritizes WikiBrain promoted fact over Cognee
result · `worker_brief` returns fewer items than `manager_brief` · `implementation_constraints`
only returns hard constraints · Temporal workflow calls `recall_context_activity`, not a memory
backend directly · memory backend failure does not fail workflow by default ·
`capture_memory_candidate` creates a pending candidate · promoted claim gets indexed into Cognee
and Graphiti · soft user preferences are not queried in core profiles.

## 17. Acceptance criteria

1. AgentConnect can run with WikiBrain, Cognee, and Graphiti configured.
2. AgentConnect can also run with any one of those backends disabled.
3. ContextBuilder produces bounded context packs for manager, worker, and reviewer profiles.
4. WikiBrain promoted claims are treated as trusted.
5. Pending claims are excluded by default.
6. Cognee provides broad retrieval but does not override WikiBrain trust state.
7. Graphiti provides temporal/supersession/project-evolution context.
8. MemoryRanker deduplicates and orders mixed backend results.
9. Temporal workflows call memory only through activities.
10. Worker workflows receive small `worker_brief` context packs.
11. Manager handoffs include trusted claims, broad relevant context, and temporal warnings.
12. Linear shows only compact memory-related updates.
13. Soft user-preference memory is not part of the core path.
14. Hard preferences/policies can be represented in AgentConnect config or WikiBrain scoped claims.

## 18. Final design rule

The memory system must not become an ungoverned pile of agent-written context.

> **AgentConnect controls access. WikiBrain controls trust. Cognee improves breadth.
> Graphiti improves temporal reasoning. Temporal runs workflows. Linear shows humans what matters.**
