# AgentConnect Handoff — Easiest Useful Level 4 Compliance Layer

> Fifth handoff, 2026-07-10. Layers on `BACKPLANE_SPEC.md`,
> `BACKPLANE_SPEC_TEMPORAL.md`, `BACKPLANE_SPEC_ADAPTERS.md`,
> `BACKPLANE_SPEC_MEMORY_STACK.md`. As-built status lives in `BACKPLANE.md`.

## Goal

Implement the easiest useful "Level 4" enforcement layer for AgentConnect.

The goal is not hardened container isolation yet. The goal is to make proprietary
agents such as Claude Code, Codex, and future manager harnesses reliably operate
through AgentConnect by default.

The compliance rule is:

> Agents may think and work inside their own harness.
> But durable work must enter AgentConnect.
> **If it is not recorded in AgentConnect, it did not happen.**

This handoff defines: `agentconnect launch`, `agentconnect shell`, task-specific
workspaces, injected agent instructions, AgentConnect-only credentials,
auto-claim, completion audit, and Linear status controlled by AgentConnect.

## 1. Intended behavior

```bash
agentconnect launch claude --task task_123 --claim
agentconnect shell --task task_123 -- claude

agentconnect launch codex --review review_456 --claim
agentconnect shell --review review_456 -- codex
```

`launch` prepares the managed session. `shell` runs the agent command inside the
prepared task-scoped environment.

The agent **should** receive: task ID, manager ID, AgentConnect API/MCP
configuration, workspace path, instructions, session token.

The agent **should not** receive: raw Temporal credentials, WikiBrain admin
credentials, Cognee write credentials, Graphiti write credentials, local model
manager credentials, cloud provider keys, rented GPU credentials, secrets manager
credentials.

## 2. Scope

This is a compliance and workflow wrapper, **not a hardened sandbox**.

It should: make AgentConnect the normal path; make bypasses visible; prevent
accidental missing records; make task state auditable; prepare for future
container/microVM sandboxing.

It does not need to: fully block hostile agents; implement microVMs; block every
network path; replace OS-level sandboxing; run every service in containers.

## 3. Core commands

### 3.1 `agentconnect launch`

Prepare a managed AgentConnect session.

Responsibilities: verify AgentConnect API is reachable; verify task/review
exists; create or reuse task workspace; create manager session record; optionally
claim task/review; write environment file; write AgentConnect instructions; write
harness-specific instruction files; write MCP/API configuration; record session
start event; print next shell command.

```
Prepared AgentConnect session.
Task: task_123
Manager: claude-code
Workspace: ~/.agentconnect/workspaces/task_123
Claim: claim_abc
Run:
  agentconnect shell --task task_123 -- claude
```

### 3.2 `agentconnect shell`

Run a command inside the AgentConnect-managed task environment.

Responsibilities: load workspace metadata; load AgentConnect session env vars;
remove dangerous backend secrets from environment; set working directory to task
workspace repo; prepend AgentConnect helper scripts to PATH; expose only
AgentConnect session credentials; run the requested command; record shell session
start/end; optionally run audit on exit.

> Do not use a fully empty environment if it breaks tools. Instead, implement a
> safe allowlist.

## 4. Workspace layout

One workspace per task or review, default `~/.agentconnect/workspaces/task_123/`:

```
workspace.json
.env.agentconnect
AGENTCONNECT.md
CLAUDE.md
CODEX.md
repo/
artifacts/
logs/
```

## 5. Repo workspace

Use a git worktree where possible.

```bash
git worktree add ~/.agentconnect/workspaces/task_123/repo agentconnect/task_123
```

Branch naming: `agentconnect/task_123/<slug>`.
Fallbacks: copy repo; bind mount existing repo; empty workspace for non-code tasks.

Workspace metadata records `workspace_id`, `task_id`, `review_id`, `manager_id`,
`repo_source`, `repo_mode`, `repo_path`, `artifact_path`, `created_at`,
`session_id`.

## 6. Generated instructions

Always generate `AGENTCONNECT.md`. Generate `CLAUDE.md` / `CODEX.md` when
applicable.

`AGENTCONNECT.md` states that AgentConnect is the source of truth for task state,
manager claims, decisions, attempts, subtasks, reviews, artifacts, approvals,
completion, and handoff summaries; that the agent must call
`get_task_context_pack` first and ensure the task/review is claimed; what to
record during work; what must hold before declaring completion; and that Temporal,
WikiBrain, Cognee, Graphiti, the local model manager, cloud model providers,
rented GPU providers, and the secrets manager must not be called directly.

> If it is not recorded in AgentConnect, it is not complete.

`CLAUDE.md` emphasizes the MCP tools and forbids relying on chat history as
canonical task state. `CODEX.md` emphasizes the CLI/API and forbids producing
final results only in chat or unregistered files.

## 7. Environment variables

`launch` writes `.env.agentconnect`:

```
AGENTCONNECT_API_URL=http://localhost:8787
AGENTCONNECT_TASK_ID=task_123
AGENTCONNECT_REVIEW_ID=
AGENTCONNECT_MANAGER_ID=claude-code
AGENTCONNECT_WORKSPACE_ID=workspace_123
AGENTCONNECT_SESSION_ID=session_abc
AGENTCONNECT_SESSION_TOKEN=token_redacted
AGENTCONNECT_MODE=manager
```

Reviewer mode sets `AGENTCONNECT_REVIEW_ID` and `AGENTCONNECT_MODE=reviewer`.

**Tools should infer task/review IDs from these env vars when omitted. This
reduces agent mistakes.**

## 8. Environment sanitization

`agentconnect shell` must remove backend credentials from the agent environment.

Denylist: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`,
`LINEAR_API_KEY`, `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`,
`TEMPORAL_CLIENT_CERT`, `TEMPORAL_CLIENT_KEY`, `WIKIBRAIN_ADMIN_TOKEN`,
`WIKIBRAIN_WRITE_TOKEN`, `COGNEE_WRITE_TOKEN`, `GRAPHITI_WRITE_TOKEN`,
`LOCAL_MODEL_MANAGER_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AZURE_CLIENT_SECRET`, `GCP_SERVICE_ACCOUNT`, `SECRETS_MANAGER_TOKEN`.

Allowlist: `PATH`, `HOME`, `SHELL`, `TERM`, `LANG`, `LC_ALL`, and the
`AGENTCONNECT_*` session vars. Allow local tool environment variables only if
explicitly configured.

## 9. AgentConnect session token

Do not give agents broad API credentials. Generate a short-lived token scoped to
task_id, review_id, manager_id, session_id, allowed actions, expiry.

Manager mode: `get_task_context_pack`, `claim_task`, `record_attempt`,
`record_decision`, `submit_subtask`, `get_subtask_status`, `list_artifacts`,
`read_artifact_chunk`, `request_review`, `release_task`.

Reviewer mode: `get_task_context_pack`, `claim_review`, `record_attempt`,
`list_artifacts`, `read_artifact_chunk`, `complete_review`.

Disallowed: admin settings, direct memory backend access, promotion of WikiBrain
memory, Temporal workflow admin, secret reads, raw worker backend access.

## 10. MCP/API configuration injection

Generate a **session-local** MCP config pointing only at AgentConnect. The
proprietary agent should see AgentConnect MCP tools, not direct backend tools.

Do not expose: `temporal_signal`, `wikibrain_promote`, `cognee_write`,
`graphiti_write`, `local_model_generate`, `secrets_read`.

## 11. Auto-claim behavior

`launch --claim` claims the task/review. If the claim fails: show current holder,
show expiry, do not launch unless `--force-readonly`. `--readonly` allows context
inspection but blocks decisions/subtasks/completion.

## 12. Completion audit

`agentconnect audit task_123` (and `--review review_456`) checks: task exists;
workspace exists; manager session exists; task was claimed; recent attempt
recorded; changed files detected; changed files registered or summarized as
artifact; important artifacts registered; open subtasks resolved; required
reviews completed; decisions recorded if durable changes happened; handoff
summary fresh; Linear sync current; memory candidates captured if configured;
task status consistent.

**Completion should require audit pass.**

## 13. Completion rule

```
agent requests completion
      ↓
AgentConnect runs audit
      ↓
if pass: mark task succeeded, update Linear
else:    reject completion with problems
```

Linear status should be updated from AgentConnect. Manual Linear status changes
should be ingested as events or requests, not treated as canonical completion.

## 14. Linear integration

The issue body should show that AgentConnect is canonical. Comments may include
`/agentconnect status`, `/agentconnect approve cloud`,
`/agentconnect request-review codex`, `/agentconnect complete`.
`/agentconnect complete` must run audit before marking complete.

## 15. Backend access policy

Proprietary agents must not receive direct write/admin access to Temporal,
WikiBrain, Cognee, Graphiti, the local model manager, cloud model providers, or
the secrets manager. All durable operations pass through AgentConnect.

## 16. Optional lightweight container mode

Not required for the first implementation, but design `agentconnect shell` so a
container runner can be added later (`--container`). Initial implementation can be
host shell only.

## 17. Data model additions

`manager_sessions` (id, task_id, review_id, manager_id, workspace_id, mode,
status, claim_id, started_at, ended_at, launch_command, shell_command,
metadata_json). Statuses: prepared, running, ended, failed, abandoned.

`workspaces` (id, task_id, review_id, path, repo_path, artifact_path, repo_mode,
created_at, destroyed_at, metadata_json).

`session_tokens` (id, session_id, token_hash, scope_json, expires_at, revoked_at,
created_at).

## 18. CLI commands

```
agentconnect launch claude --task TASK_ID --claim
agentconnect launch codex --review REVIEW_ID --claim
agentconnect shell --task TASK_ID -- COMMAND...
agentconnect shell --review REVIEW_ID -- COMMAND...
agentconnect audit TASK_ID
agentconnect audit --review REVIEW_ID
agentconnect sessions list
agentconnect sessions show SESSION_ID
agentconnect workspaces list
agentconnect workspaces show WORKSPACE_ID
```

Optional: `agentconnect complete TASK_ID`, `agentconnect complete --review
REVIEW_ID`, `agentconnect cleanup TASK_ID`, `agentconnect cleanup --abandoned`.

## 19. Tests

launch verifies task exists · launch creates workspace · launch creates
`.env.agentconnect` · launch generates `AGENTCONNECT.md` · launch generates
`CLAUDE.md` for claude manager · launch generates `CODEX.md` for codex manager ·
launch creates manager session · `launch --claim` claims task · `launch --claim`
fails if task already claimed · shell loads workspace env · shell sanitizes
backend secrets · shell sets working directory to workspace repo · shell exposes
`AGENTCONNECT_SESSION_TOKEN` · shell records session start/end · MCP config
exposes only AgentConnect tools · audit fails if no attempt recorded · audit fails
if changed files are unregistered · audit fails if required review incomplete ·
audit passes when attempts/artifacts/reviews are complete · complete refuses task
if audit fails · complete updates AgentConnect and then Linear if audit passes ·
session token cannot call admin/memory promotion/Temporal endpoints.

## 20. Acceptance criteria

1. A user can run `agentconnect launch claude --task task_123 --claim`.
2. A task-specific workspace is created.
3. `AGENTCONNECT.md` and `CLAUDE.md` are generated.
4. AgentConnect MCP/API config is injected.
5. The task is claimed automatically.
6. `agentconnect shell --task task_123 -- claude` runs inside the workspace.
7. Backend secrets are absent from the agent environment.
8. The agent has only AgentConnect-facing credentials/tools.
9. The agent can call `get_task_context_pack` without manually entering task_id.
10. The agent can record attempts/decisions/subtasks through AgentConnect.
11. Completion requires `agentconnect audit`.
12. Audit detects unregistered changed files and missing attempts.
13. Linear task status is updated only after AgentConnect completion.
14. Direct backend tools are not exposed to the proprietary agent by default.

## 21. Final design rule

> The easiest useful Level 4 is: `launch` + `shell` + task-specific workspace +
> injected instructions + AgentConnect-only tools/credentials + auto-claim +
> completion audit.
>
> Do not start with full hardened sandboxing. **Make AgentConnect the normal path
> first.**
