# Safety

AgentConnect includes **baseline local safety scanning** for artifact ingest and
context-pack output. It is local, deterministic, and dependency-free: no HTTP service, no
LLM classifier, no third-party guard package.

Before anything else, the four things it does **not** do:

* **It is not a sandbox.** It reads content; it does not contain a process.
* **It does not stop direct SQLite, file, or environment tampering.** An agent that opens
  the ledger with the `sqlite3` binary, edits an artifact on disk, or unsets
  `AGENTCONNECT_MODE` is stopped by nothing here. That needs OS-level isolation — a
  container, a microVM, a separate user account.
* **It does not prove content is true.** A claim that passes every rule is still just a
  claim. Trust is a separate axis, and it is WikiBrain's, not the scanner's.
* **It only scans AgentConnect-controlled surfaces.** Content that never passes through
  `create_artifact` or a context pack is never seen.

What it does: scan, redact, withhold, and label risky content at the surfaces AgentConnect
owns, before that content becomes durable evidence or reaches an agent.

## What protects you today

* **Baseline content scanning** at two surfaces, described below.
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

## The module

`agentconnect.safety`, shipped inside `agentconnect-core`. It imports nothing outside the
standard library, and a test asserts that.

```
agentconnect.safety
  models.py       Decision, RiskLevel, Category, Finding, SafetyResult, SafetyItem
  scanner.py      scan_text(content, *, surface, policy) / scan_items(items, *, policy)
  redaction.py    span merging + `[REDACTED:<category>:<rule_id>]` markers
  policies.py     (category, risk) -> decision, per surface
  rules/
    secrets.py            API keys, tokens, private keys, JWTs, .env assignments
    prompt_injection.py   text that addresses the agent instead of informing it
    tool_instructions.py  directives aimed at the agent's tools
    encoding.py           long opaque blobs the other rules cannot see into
```

Decisions, weakest to strongest: `allow`, `warn`, `redact`, `quarantine`, `block`. The
strongest decision across a scan's findings is the scan's decision.

### Fail closed

**A scanner that failed has not found the content clean.** Each ruleset runs in its own
`try`. A rule that raises produces a `scanner_error` finding, and every policy maps that
to `quarantine` — never `allow`. Artifacts whose scan blew up are stored with
`safety_scanner_failed: true`; context items whose scan blew up are withheld.

This is deliberate and it is the property most worth keeping. A broad `except` that
returned an empty result would report "no findings", and every reader downstream would
take that as a clean bill of health.

### Findings never carry the matched text

A finding travels into artifact metadata, into logs, and into pack warnings. One that
quoted the secret would copy it to three new places while announcing it had been removed
from one. Findings carry a rule id, a category, a risk level, a message, and a span.

## Surface 1 — artifact ingest

Policy `artifact_ingest`, applied in `AgentConnectService.create_artifact`, **before** the
body is written to the artifact store.

| Finding | Decision |
|---|---|
| probable secret | `redact` — the marker replaces the credential, the artifact is stored |
| high-risk tool directive (exfiltration, `curl \| sh`, `rm -rf /`) | `quarantine` |
| prompt injection | `warn` — an artifact quoting an injection is legitimate evidence |
| long encoded blob | `warn` |

The artifact is **never destroyed**. It is the record that the work happened, and deleting
it to protect a credential would also delete the evidence that the credential was ever
there. A quarantined artifact is stored, marked, and still readable by an operator.

Safety metadata is written onto the artifact, and only when something was found — a clean
artifact carries none, so the metadata stays useful as an alert:

```
safety_decision  safety_risk_level  safety_findings  safety_policy_version
safety_redacted  safety_warnings    safety_scanner_failed
```

## Surface 2 — context-pack output

Policy `context_output`, applied in `ContextBuilder` to recalled memory items, **before**
the pack is returned to a manager, worker, or reviewer.

| Finding | Decision |
|---|---|
| probable secret | `redact` — the claim survives, the credential does not |
| high-risk prompt injection | `quarantine` — withheld from the pack |
| high-risk tool directive | `quarantine` — withheld from the pack |
| medium-risk injection or tool directive | `warn` — delivered, with a `safety:*` label |
| long encoded blob | `warn` |

**Nothing is dropped silently.** Redacting or withholding adds a pack-level warning:

```
1 context item was redacted by AgentConnect safety scanning.
1 context item was withheld by AgentConnect safety scanning.
```

A pack that quietly got shorter is indistinguishable from a pack that had nothing to say,
and the second is the reading an agent will make.

Only *recalled memory* is scanned. Ledger truth — locked decisions, hard policy — is
AgentConnect's own record; redacting a decision would corrupt the thing the audit relies
on.

### Two layers, not one

The ranker demotes an untrusted item and labels it; the scanner reads its text and may
withhold it. Neither is redundant: ranking cannot read content, and the scanner cannot
judge authority. An unremarkable low-authority document is delivered, ranked last. The
same document telling the agent to *send all secrets to* somewhere never arrives.

## Turning it off

`AgentConnectService(safety_enabled=False)`. A constructor argument rather than a config
file, because it should be hard to do by accident. With it off, neither surface is
scanned and neither carries metadata saying so.

## Future work

Not implemented. Named here so the shape is agreed before anyone builds it.

* **More surfaces.** `subtask_instruction`, `review_input`, `attempt_decision_notes` are
  named in `policies.py` and have no decision table; `policy()` refuses them rather than
  guessing.
* **PII.** A `rules/pii.py` is in the designed shape and does not exist. Names, emails,
  and addresses need a different precision/recall trade-off than credentials do, and
  getting it wrong makes the layer noisy enough to be switched off.
* **Redaction of ledger surfaces** — decisions and attempts — which requires answering
  what an audit means when its evidence has been rewritten.

## Legacy

`agentconnect-router`, in the advanced routing stack, carries an optional hook for an
external guard package. It is a soft dependency (absent, every function is a no-op),
dormant unless an environment flag is set, and enforcement is separately opt-in. It is
**not** part of the managed coding-agent loop, it is not required, and `agentconnect.safety`
does not use it.

## Related

* [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) — the trust boundary, stated before the commands.
* [STATUS.md](STATUS.md) — the current checkpoint, and what the test suite does not prove.
