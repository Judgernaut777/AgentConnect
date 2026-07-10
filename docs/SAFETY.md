# Safety

AgentConnect scans content at the surfaces it owns, before that content becomes
durable evidence or reaches an agent. **AgentConnect owns policy and enforcement.
Detection is modular**, and most of it is delegated to maintained third-party tools.

```
AgentConnect-controlled surface
  → AgentConnect safety policy
    → configured safety engines
      → normalized findings
        → AgentConnect enforcement
```

Before anything else, the four things this does **not** do:

* **It is not a sandbox.** It reads content; it does not contain a process.
* **It does not stop direct SQLite, file, or environment tampering.** An agent that
  opens the ledger with the `sqlite3` binary, edits an artifact on disk, or unsets
  `AGENTCONNECT_MODE` is stopped by nothing here. That needs OS-level isolation.
* **It does not prove content is true.** A claim that passes every rule is still a
  claim. Trust is a separate axis, and it is WikiBrain's, not the scanner's.
* **It only scans AgentConnect-controlled surfaces.** Content that never passes
  through `create_artifact` or a context pack is never seen.

## Who decides what

| AgentConnect owns | Engines own |
|---|---|
| when scanning happens | secret detection |
| which engines run at each surface | PII detection |
| how findings are combined | prompt-injection classification |
| allow / warn / redact / quarantine / block | repository and file scanning |
| what is persisted, and what reaches an agent | upstream rule and model maintenance |
| what appears in audit metadata | |

No engine decides task state, artifact persistence, audit success, or context-pack
inclusion. Engines are handed text and return findings. Findings are normalized into
AgentConnect's own model before policy ever sees them.

## Engines

| Engine | Capabilities | Install | Status |
|---|---|---|---|
| `baseline` | secrets, prompt_injection, tool_control, encoded_content | built in, always on | **Default.** Standard library, offline, deterministic. |
| `detect_secrets` | secrets | `pip install "agentconnect-core[safety-secrets]"` | Integrated; its tests run against the real library and **skip** when the extra is absent, as in the default gate. |
| `gitleaks` | secrets, repository_secrets | install the `gitleaks` binary | Integrated and tested against the real binary. |
| `trufflehog` | secrets, repository_secrets | install the `trufflehog` binary | Integrated and tested against the real binary. |
| `presidio` | pii | `pip install "agentconnect-core[safety-pii]"` + a spaCy model | Adapter implemented, fake-backed tests. **Not** exercised against an installed Presidio in this gate. |
| `gliner` | pii | `pip install "agentconnect-core[safety-pii-gliner]"` | Adapter implemented, fake-backed tests. **Not** exercised against real weights. |
| `prompt_guard` | prompt_injection | `pip install "agentconnect-core[safety-injection]"` | Adapter implemented, fake-backed tests. **Not** exercised against real weights. |

`safety-all` installs every Python-managed engine. TruffleHog and Gitleaks are
external binaries by design, not pip dependencies.

**The default install carries the baseline and nothing else.** No torch, no model
weights, no subprocess. `import agentconnect.safety` does not import any optional
library, and a test asserts it.

### The baseline is a floor, not the architecture

Standard library, deterministic, offline. It exists so a default install is never a
no-op, so the gate runs fast, and so there is a fallback when a maintained engine is
unavailable. It is **not** enterprise-grade, and it should not grow into a
replacement for detect-secrets, TruffleHog, Gitleaks, Presidio, or a maintained
classifier. New baseline rules are justified only by safe defaults, offline
operation, regression coverage, or redaction infrastructure.

Its **PII coverage is deliberately nil**. A homegrown regex for names and addresses
catches the easy third, misses the rest, and the coverage it appears to provide is
the reason nobody installs a real engine. Enable Presidio.

## Configuration

Copy `config/safety.yaml.example` to `config/safety.yaml`, or point
`AGENTCONNECT_SAFETY_CONFIG` at it.

```yaml
safety:
  enabled: true
  engines:
    baseline:       { enabled: true, required: true }
    detect_secrets: { enabled: true, use_entropy_plugins: false }
    trufflehog:     { enabled: false, executable: trufflehog,
                      timeout_seconds: 20, allow_network_verification: false }
    presidio:       { enabled: false }
```

* **An unknown engine name is a startup error**, not a warning. An operator who typed
  `detect_secrests:` believes an engine is running.
* **Enabled but not installed** is a visible `unavailable` status — from
  `pipeline.status()` at startup, and a warning on every scan. Never an import crash.
* **No engines configured** means the baseline still runs.

### `required` versus optional

A `required` engine that is missing or broken **fails the scan closed**. An optional
one degrades it: the engines that did run still have their say, a warning is emitted,
and the result never reads as `allow`.

## Engine selection by surface

Policy owns the mapping. Engines are chosen by *capability*, so adding an engine
never means editing a surface, and an engine can never select itself onto one.

| Surface | Capabilities requested |
|---|---|
| `artifact_ingest` | secrets, repository_secrets, pii, prompt_injection, tool_control, encoded_content |
| `context_output` | secrets, pii, prompt_injection, tool_control, encoded_content |
| `repository_scan` | repository_secrets |

Artifacts are files, so a subprocess is worth it and the repository-secret engines
apply. Context items are short prose recalled in bulk: a subprocess per item is a
poor trade, so TruffleHog and Gitleaks are not selected there. `surfaces:` in the
config overrides this per surface.

## Aggregation

Findings from all configured engines are **unioned**. An engine that found nothing
has abstained, not vetoed: if the baseline is silent and detect-secrets names a
GitHub token, there is a GitHub token.

Overlapping findings in the same category are collapsed into **one** redaction —
emitting two would nest markers and corrupt every offset after. The survivor takes
the strongest risk level, the widest span, and the highest confidence; the losers are
recorded in `metadata["also_detected_by"]`, so attribution is never lost. Two engines
agreeing is evidence; one engine's false positive is a bug report against that engine.

**Findings never carry the matched value.** A finding travels into artifact metadata,
logs, and pack warnings. Engines hand us their raw match; the adapter uses it to
locate a span and drops it.

## Failure behavior

Five engine states, and no two of them collapse:

| State | Meaning | Effect |
|---|---|---|
| `ok` | it read the content | its findings count |
| `ok`, no findings | it read the content and found nothing | contributes nothing |
| `skipped` | disabled, or lacks a capability the surface wants | nothing |
| `unavailable` | never installed; it never looked | required → fail closed; optional → warn |
| `failed` / `timeout` | present, and it raised | required → fail closed; optional → never `allow` |

**A scanner that failed has not found the content clean.** That is the invariant the
whole layer rests on. A broad `except` returning an empty result would report "no
findings", and every reader downstream would take it as a clean bill of health.

Per surface:

* `context_output` — a failing required engine withholds the item. Baseline findings
  are not overridden by an enhanced engine's failure.
* `artifact_ingest` — engine failure never marks content clean; the artifact is
  stored, quarantined, and flagged `safety_scanner_failed`.
* An optional engine's failure warns and degrades; it does not withhold on its own.

One more, easy to miss: **a credential that cannot be located is withheld, not
redacted.** An engine may report a secret without a usable offset. Emitting a marker
then would announce that the secret was handled while it sat in the text.

## Surface 1 — artifact ingest

Applied in `AgentConnectService.create_artifact`, **before** the body reaches the
artifact store.

| Finding | Decision |
|---|---|
| probable secret | `redact` — marker replaces the credential, the artifact is stored |
| PII (medium/high) | `redact` |
| high-risk tool directive (exfiltration, `curl \| sh`, `rm -rf /`) | `quarantine` |
| prompt injection | `warn` — an artifact quoting an injection is legitimate evidence |
| long encoded blob | `warn` |

The artifact is **never destroyed**. It is the record that the work happened, and
deleting it to protect a credential would delete the evidence the credential was ever
there. Safety metadata is written only when something was found:

```
safety_decision  safety_risk_level  safety_findings  safety_policy_version
safety_redacted  safety_warnings    safety_scanner_failed  safety_engines
```

## Surface 2 — context-pack output

Applied in `ContextBuilder` to recalled memory items, **before** the pack is returned
to a manager, worker, or reviewer.

| Finding | Decision |
|---|---|
| probable secret | `redact` — the claim survives, the credential does not |
| PII (medium/high) | `redact` |
| high-risk prompt injection | `quarantine` — withheld |
| high-risk tool directive | `quarantine` — withheld |
| medium-risk injection or tool directive | `warn` — delivered, with a `safety:*` label |
| long encoded blob | `warn` |

**Nothing is dropped silently:**

```
1 context item was redacted by AgentConnect safety scanning.
1 context item was withheld by AgentConnect safety scanning.
```

A pack that quietly got shorter is indistinguishable from a pack that had nothing to
say, and the second is the reading an agent will make.

Only *recalled memory* is scanned. Ledger truth — locked decisions, hard policy — is
AgentConnect's own record; redacting a decision would corrupt what the audit rests on.

### Two layers, not one

The ranker demotes an untrusted item and labels it; the scanner reads its text and
may withhold it. Neither is redundant: ranking cannot read content, and the scanner
cannot judge authority.

## Turning it off

`AgentConnectService(safety_enabled=False)` — a constructor argument rather than a
config key, because it should be hard to do by accident.

## Future work

* **More surfaces.** `subtask_instruction`, `review_input`, `attempt_decision_notes`
  are named in `policies.py` and have no decision table; `policy()` refuses them
  rather than guessing.
* **Containment / spotlighting** for `context_output`. See the note below.
* **Redaction of ledger surfaces** — decisions and attempts — which requires deciding
  what an audit means when its evidence has been rewritten.

## A known interaction: detect-secrets and WikiBrain

Installing `detect-secrets` into a shared environment changes WikiBrain's behavior.
WikiBrain runs its own promotion-time safety check through detect-secrets **with the
entropy plugins on**, and those plugins fire on ordinary English words — `token`,
`Refresh`, `lives`, `validation` are all flagged as `Base64HighEntropyString`. The
result is that WikiBrain refuses to promote plain-prose claims once the library is
present.

That is why this repository's gate runs with the extra absent, and why AgentConnect's
adapter disables the entropy plugins unless an operator asks for them. If you enable
`safety-secrets` alongside WikiBrain, expect promotion refusals until WikiBrain's own
plugin set is narrowed. WikiBrain is frozen; nothing here changes it.

## Notes on reuse

The engine adapters were informed by an earlier standalone guard package by the same
author (no LICENSE file; the code is the author's own). Ports were rewritten to
AgentConnect's finding model and policy boundary rather than copied. Two upstream bugs
were found and not carried over:

* its detect-secrets adapter calls `scan_line` outside `default_settings()`, where the
  function returns an empty iterator — the adapter detects nothing, silently, forever;
* it redacts using detect-secrets' `secret_value`, which for GitHub tokens is the
  three-character prefix `ghp` — masking three characters and leaving the token.

Containment (spotlighting untrusted content inside a nonce-delimited fence) is
**deferred**, not rejected. Three reasons. AgentConnect never renders one prompt
string — a context pack is structured, and its items already carry
`memory_is_external_context` and `trusted=false`, which is the same signal
structurally. Wrapping item text would corrupt the spans that redaction and
aggregation depend on. And the upstream nonce is `sha256(source_id + content)` with no
secret salt, so an attacker who knows the content can compute the fence and close it;
adopting it would need a per-process secret or a per-pack nonce first.

## Related

* [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) — the trust boundary, stated before the commands.
* [STATUS.md](STATUS.md) — the current checkpoint, and what the test suite does not prove.
