# ADR 0010 — Control-model plane ownership, naming, and the vocabulary projection

Status: accepted (2026-08-25)

## Context

A fine-tuned orchestration controller exists and is finished: `qwen3-4b-control-v12`,
a Qwen3-4B-Instruct-2507 LoRA (33M trainable params) quantized to Q4_K_M (2.4 GB,
~3.0 GiB resident) for CPU deployment. It emits one strict JSON decision per call
across seven modes — CLASSIFY, ROUTE, ASSIGN, REVIEW_PLAN, MONITOR, RECOVER,
NORMALIZE — each carrying `reason_codes` (62-code controlled vocabulary),
`detected_constraints` (12 codes), and an `escalate` flag. Its own model card is
unambiguous about its standing: *"Not a policy authority. Deterministic code must
enforce every hard constraint regardless of model output."* It was evaluated
independently (0 hard-policy violations on a 499-example adversarial split,
escalation recall 1.00, quantization lossless for schema validity and decision
accuracy) and its authors recommended advancing it to **shadow mode**, explicitly
not to production.

Three facts about its current placement forced this ADR.

**It is named for the wrong plane.** The repository is
`Judgernaut777/brainconnect-control-model` and its documents describe it as a
controller "for BrainConnect." Six of its seven modes are not Knowledge-plane
concerns at all:

| mode | what it decides | plane that owns that |
|---|---|---|
| ROUTE | `route_class`, `review_class`, `parallelism` | Work (this repo's `RoutingEngine`) + Compute |
| ASSIGN | `agent_roles[]`, `ownership_constraints[]`, reviewer | Work (`core/subtasks.py`, model-manager profiles) |
| REVIEW_PLAN | approve / revise / reject a plan | Work (`core/reviews.py`) |
| MONITOR | healthy / stalled / context_degraded / … | Work (worker runtime, observability) |
| RECOVER | retry / compact_context / change_worker_class / … | Work (worker runtime) |
| CLASSIFY | task family, complexity, risk, privacy | Work (`common/privacy.py` classification) |
| NORMALIZE | canonicalize a malformed record | arguably Knowledge |

**BrainConnect has already ruled on this, in its own voice.** BrainConnect
ADR 0008 (*The BrainConnect Orchestration Boundary*, accepted 2026-07-13 — the
same day the control model's final commit landed) decides that BC "REASONS about
capabilities and DELEGATES mechanism… it never re-holds live state, placement
math, routing math, the plan/execution ledger, or the observability stream," and
lists among its binding prohibitions: *"Do NOT duplicate AgentConnect's router,
delegation, governor, or observability model."* Its delegation table sends the
capability-based router, the swap-minimizing scheduler, worker orchestration,
multi-model roles, and independent verification to AgentConnect. A model named
for BrainConnect whose dominant output is a routing decision sits directly across
that boundary. This ADR does not overrule BC ADR 0008 — it completes it by naming
the plane the artifact was already delegated to.

**It is wired to nothing.** A search of all six ecosystem repositories finds four
references to the control model, all of them marketing copy in
`Connect/website/DESIGN_HANDOFF.md`. The six-step integration handoff in the
model's own `reports/final-recommendation.md` has never been executed. Nothing
depends on the current name, which is why renaming is cheap today and will not be
in a month.

A fourth fact shapes the second half of this ADR. Shadow mode's headline metric is
agreement between the control model's ROUTE decision and the deterministic
router's `RoutingDecision` — and that comparison is not currently computable. Four
privacy vocabularies are in play, and no mapping between them exists:

| vocabulary | values | used by |
|---|---|---|
| control model `PRIVACY` | `public, local_preferred, local_only, secret_involved` | the model's input and output |
| `PrivacyClass` | `public, low_sensitive, repo_sensitive, secret_sensitive, restricted` | `router/routing.py` (payload classification) |
| `PrivacyTier` | `public, public_redacted, repo_sensitive, secret_sensitive, local_only` | `core/routing.py`, subtasks; mirrored verbatim by ComputeConnect |
| `ProviderPrivacyTier` | `local_only, private_rented, external, external_paid` | what a provider *is* |

Written ad hoc at the comparison site, a mapping between these is exactly where a
privacy widening would hide — and one specific gap guarantees a wrong answer if
guessed at: `private_rented` (your own weights on rented hardware) has **no**
counterpart in the control vocabulary at all.

## Decision

### 1. The control model is a Work-plane artifact, named `connect-control-model`

It is renamed from `brainconnect-control-model`. AgentConnect owns its
integration, its vocabulary projection, and its shadow-mode evaluation.
BrainConnect owns none of it. The rename of the GitHub repository itself is an
out-of-band step (see *Out-of-band* below); everything in this repository already
uses the new name.

### 2. It never runs inside BrainConnect, and BrainConnect's determinism boundary is untouched

BrainConnect's stated property is precise and stays exactly as it is: the
`brainconnect` CLI makes zero model calls, and the separate `brainconnect-librarian`
process is the only part of that product that uses a model. This ADR adds nothing
to BrainConnect and removes nothing from it.

### 3. It is advisory, and its output never enters a governed Decision Record

The model's own card already forbids it authority; this ADR fixes where that
boundary is enforced. A control-model decision is an **observation recorded
alongside** a routing decision, never an input the deterministic router or the
Connect-Governance Decision Kernel evaluates. The Kernel's invariants require
byte-identical decisions from identical inputs, and llama.cpp is not
bit-reproducible across builds and thread counts — so even at `temperature=0`
with a pinned GGUF hash, model output must stay out of the replayable path.

### 4. It is consumed out-of-band, never inline

Measured decision latency is 5.46 s at 16 threads, memory-bandwidth-bound (8→16
threads moves throughput only 14.6→16.6 tok/s, so more cores will not fix it).
`POST /route/decide` is a side-effect-free query that BrainConnect's
`HttpRoutingClient` calls synchronously; putting a ~5 s model call in front of it
would be a per-decision regression for a decision that already answers
deterministically. Shadow mode therefore consumes the ecosystem event bus — which
Connect already defines as best-effort and "a projection, never a system of
record" — and emits `(normalized_state, model_decision, router_decision, outcome)`
records for offline comparison.

### 5. AgentConnect owns the vocabulary projection, and a projection never widens permission

`agentconnect.core.control_projection` is the single place any control-model
vocabulary is converted. Its rules:

- Where a source value has no exact counterpart, it maps to the **strictest**
  compatible target, never the loosest or the closest-looking. `local_preferred`
  projects onto `PrivacyTier.repo_sensitive`, *not* `public_redacted` — the latter
  is one of ComputeConnect's two cloud-permitting tiers, so that mapping would
  hand cloud eligibility to a task that never asked for it.
- A projection that cannot be made faithfully **says so** (`Projection.faithful
  is False`) rather than guessing. This is the same posture as
  `computeconnect.placement`'s structured refusal, for the same reason.
- `private_rented` is unrepresentable in the control vocabulary and is scored as
  `Agreement.unrepresentable`, never `disagree`. Scoring a rented route as a model
  miss would understate shadow-mode agreement for a hole in the vocabulary rather
  than a fault in the model's decision.
- Every `cloud_*` class admits **both** `external` and `external_paid`: the model
  receives no cost or quota signal, so collapsing to one manufactures a
  distinction it never saw.
- The module is pure — no I/O, no clock, no randomness, no config — and holds no
  authority. Hard constraints stay where they are, enforced by the deterministic
  routers against vocabularies they resolved themselves.

The non-widening rule is enforced by property tests over every value of all four
vocabularies and the full capability-class × provider-tier cross product, not by
spot checks.

### 6. Capability families are extracted, not invented

AgentConnect's capability names are operator configuration (`profiles.yaml`), not
a fixed enum. `capability_family()` returns the bare family string the control
model's class name encodes (`general`, `repository_coder`, `reasoner`, `vision`,
`frontier`) and stops there. Mapping a family onto a deployment's configured
capability strings is the caller's job; a fixed table here would be this module
inventing policy it has no standing to set.

## Consequences

**Positive.** The artifact lands in the plane that already owns routing, roles,
review, and worker runtime, so integration reuses shipped surfaces instead of
duplicating them across a plane boundary BC ADR 0008 explicitly closed.
BrainConnect's determinism claim stays clean and unqualified. Shadow-mode
agreement becomes computable, and computable *safely* — the comparison cannot
silently widen a privacy tier, because the only conversion path is tested against
that. The `private_rented` gap is now a visible, named limitation instead of a
number quietly wrong in a report.

**Cost.** The GitHub repository rename is manual and breaks existing clone URLs
(GitHub redirects, but pinned remotes should be updated). Documents inside the
control-model repository still say "BrainConnect" and will need a pass in that
repository — this ADR cannot make that edit from here.

**Deferred.** The control model has no cost, quota, or rental inputs, so its ROUTE
can never fully reproduce `RoutingEngine`'s — which weighs `cost_penalty`,
`quota_scarcity_penalty`, `budget_pressure_penalty`, and `rental_setup_penalty`.
Whether to extend the control schema to v2 with those inputs is a decision for
after shadow mode reports how much disagreement they actually account for. It is
not decided here.

**Not decided here.** Whether the control model is ever promoted past shadow mode;
whether ASSIGN grows into a subtask DAG author; whether the ecosystem manifest
should pin the control model as a product (it ships no installable package, so it
is currently out of the manifest's scope).

## Alternatives considered

**Leave it in BrainConnect as a librarian-style sidecar.** The letter of
BrainConnect's invariant would survive — the librarian precedent shows a separate
model-using process is permitted there. It fails on plane ownership rather than on
the invariant: BC ADR 0008's binding prohibitions send routing, roles, worker
orchestration, and the observability model to AgentConnect, and a BC-resident
ROUTE advisor sits across that line. It would also put BC in the position of
advising on decisions it is forbidden to make.

**Give the control model its own plane.** Rejected. Connect's thin-control-plane
principle already says the coordinating plane is never a fifth authority competing
with the four infrastructure planes; a sixth plane for an advisory model would be
exactly that, for an artifact that is explicitly not an authority.

**Project the vocabularies at each call site.** Rejected — it is the same mapping
written repeatedly by different callers with different assumptions, and privacy
widenings do not announce themselves. One tested module with a stated widening
rule is the whole point.

**Force `private_rented` onto the nearest class.** Rejected in both directions:
scored as an `inference_box` match it reports agreement the model never expressed;
scored as a mismatch it charges the model for a class it cannot name. A structured
gap is the honest answer.

## Out-of-band

Renaming the GitHub repository `brainconnect-control-model` →
`connect-control-model` is a repository-settings operation and is not performed by
this change. Until it happens, `CONTROL_ONTOLOGY_SOURCE` in
`control_projection.py` names the file by its new repository name; the ontology it
points at is byte-identical either way, and the mirrored enums are pinned by test.
