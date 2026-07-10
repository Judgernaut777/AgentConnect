# Safety — what exists today, and where it is going

AgentConnect is a **compliance and control layer, not a security sandbox.** That sentence
governs this whole document. Read it before the rest.

## What protects you today

This is the complete list. Nothing here scans content.

* **Managed-shell environment sanitization.** The agent's environment is built from an
  allowlist. Backend credentials are not removed so much as never copied in.
* **No backend credentials are forwarded into the managed shell.** `AGENTCONNECT_DB_PATH`
  and the other `AGENTCONNECT_*` config pointers *are* forwarded, deliberately, so the
  agent's own tools reach the operator's ledger rather than a private fallback. They are
  paths and knobs; they grant no cloud spend, no model access, no backend write token.
* **`AGENTCONNECT_MODE` restricts managed-session CLI commands.** Inside a managed
  session the CLI refuses `complete` and `memory promote`.
* **Agent tokens cannot complete tasks.** `complete_task` is in no session mode's action
  list, so MCP and HTTP deny it structurally.
* **The audit checks evidence.** It asks where the work is in the ledger, and it writes
  nothing while asking.

## What does not protect you today

**There is no content scanner in the managed loop.** No secret detection, no PII
redaction, no prompt-injection detection, no quarantine. An artifact containing a leaked
credential is stored as-is. A context pack containing injected instructions is handed to
the agent as-is.

**AgentConnect is not a sandbox.** An agent that unsets `AGENTCONNECT_MODE`, opens the
SQLite ledger directly, or edits a file on disk is stopped by nothing here — and would not
be stopped by the future scanner below either. Content scanning inspects content; it does
not contain a process. Guarding against direct tampering needs OS-level isolation: a
container, a microVM, a separate user account.

*(One historical exception, in the advanced routing stack rather than the managed loop:
`agentconnect-router` carries an optional, dormant-by-default hook for an external guard
package. It is a soft dependency — absent, every function is a no-op — and scanning runs
only when its environment flag is set. It is not part of the managed coding-agent loop,
it is not required, and it is not the direction described below.)*

## Future direction: AgentConnect-local safety scanning

**This is future work. None of it is implemented.** It is written down so the shape is
agreed before anyone builds it, not to describe anything you can use.

AgentConnect should include baseline local safety scanning **directly in AgentConnect**,
scoped to AgentConnect-owned surfaces: artifacts, context packs, subtask instructions,
review input and output, attempts, and decisions.

The layer should detect and handle secrets, PII, prompt-injection text, unsafe
tool-control instructions, and suspicious encoded blobs — **before** content is injected
into an agent or stored as task evidence.

It must be an **AgentConnect-local module**, not a required third-party runtime
dependency. AgentConnect must continue to work standalone.

### Proposed shape

```
agentconnect.safety
  models.py
  scanner.py
  redaction.py
  policies.py
  rules/
    secrets.py
    pii.py
    prompt_injection.py
    tool_instructions.py
    encoding.py
```

### Proposed policies

Each names an AgentConnect-owned surface, because a policy that cannot name the surface it
guards will be applied inconsistently.

| Policy | Surface |
|---|---|
| `artifact_ingest` | content becoming a durable artifact |
| `context_output` | a context pack, before a manager, worker, or reviewer receives it |
| `subtask_instruction` | instructions handed to a bounded worker |
| `review_input` | material submitted for review |
| `attempt_decision_notes` | free text on attempts and decisions |

### Expected behavior

* artifact carrying a secret → redacted or quarantined **before** context injection
* prompt-injection text in an artifact → warning or quarantine **before** the context pack
* subtask instruction with a severe unsafe directive → block or warn
* review output → scanned before it becomes durable task evidence
* context pack → scanned before a manager, worker, or reviewer receives it

### What it will still not do

Contain a hostile process. Stop direct SQLite, filesystem, or environment tampering.
Replace OS-level isolation. Make AgentConnect a sandbox.

## Related

* [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) — the trust boundary, stated before the commands.
* [STATUS.md](STATUS.md) — the current checkpoint, and what the test suite does not prove.
