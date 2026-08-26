# Control-model shadow mode

How the control model (`connect-control-model`, [ADR 0010](adr/0010-control-model-plane-ownership.md))
earns — or fails to earn — a promotion past shadow mode.

Shadow mode watches routing decisions the deterministic router has **already
made**, asks the model what it would have done, and records the pair. The model
decides nothing. Nothing waits on it. Module:
`agentconnect.core.control_shadow`.

## The record

One record per routing event, the quadruple the model's own handoff asks for:

```json
{
  "seq": 412,
  "task_id": "task_...", "subtask_id": "subtask_...",
  "state":  {"privacy": "local_only", "needed_capabilities": ["python"], "...": "..."},
  "model":  {"route_class": "inference_box_general", "confidence": 0.94,
             "reason_codes": ["local_capable"], "escalate": false},
  "model_error": null,
  "router": {"decision": "routed", "selected_provider": "w1",
             "provider_tier": "local_only", "policy_version": "v7"},
  "agreement": "agree",
  "outcome": "succeeded"
}
```

`state` is bounded by `SHADOW_STATE_KEYS` and contains **no prompt, transcript,
task id, or free-form field** — the same rule the event bus applies to its own
payloads, checked rather than trusted. `model` is `null` whenever the model was
unreachable or its reply failed the schema twice; `model_error` says which.

## Wiring

```python
from agentconnect.core.control_shadow import (
    HttpControlModelClient, MemoryShadowSink, PLACED_EVENT_TYPE, ShadowConsumer, summarize,
)
from agentconnect.core.control_shadow_resolver import LedgerRoutingFactsResolver

consumer = ShadowConsumer(
    events=storage,          # anything with list_bus_events(since=, limit=, types=)
    resolver=LedgerRoutingFactsResolver(storage),   # see below
    client=HttpControlModelClient(
        base_url="http://127.0.0.1:8099", model="qwen3-4b-control-v12",
        system_prompt=SYSTEM_PROMPT,        # from the model repo's gen_dataset.py
    ),
    sink=MemoryShadowSink(),
    event_types=(PLACED_EVENT_TYPE,),
)

run = consumer.run_once(limit=50)           # never raises
print(summarize(sink.records).to_dict())
```

Serve the model per its handoff: `llama-server -ngl 0 -t <cores> -c 8192` on the
Q4_K_M GGUF. The client already pins `temperature=0`, `max_tokens=192`, and a
stop string — the model card's "may fail to stop under OOD/adversarial" makes
those load-bearing, not tuning knobs.

**Run it out-of-band** — a timer, a CLI invocation, a batch job. Never from a
request path. A test asserts that no module under `packages/` names
`control_shadow` except shadow mode's own, so an inline call fails CI rather
than review.

## The resolver

`LedgerRoutingFactsResolver` (`agentconnect.core.control_shadow_resolver`) turns
a placement event into a `ShadowInput` by reading the ledger:

```python
from agentconnect.core.control_shadow_resolver import LedgerRoutingFactsResolver

resolver = LedgerRoutingFactsResolver(svc.storage, policy_version="v1")
```

It lives in its own module because `control_shadow` deliberately holds no
ledger — that is what keeps the evaluator a pure function whose tests never open
a database.

**It reads the routing decision, not the bus payload.** A `compute.placed`
payload does name the placement class, and trusting it would be one line
shorter. The ledger is read instead because `Subtask.route_reason` is the
persisted `RouteExplanation` the router itself wrote, and EVENT_BUS.md §0 is
explicit that the bus is "never authoritative for anything."

That is not academic. Until the fix shipped alongside this module,
`compute.placed` reported `location: "local"` for **every** route, cloud and
rented included — the emit read `explanation.selected_location`, a field that
did not exist on the model, and fell through to a literal default. A resolver
trusting that payload would have scored every cloud route as a disagreement and
quietly understated the model. A test now pins the payload against a lie.

Every failure is a skip, never an exception: no subtask id, a reaped subtask, a
subtask with no recorded route, a route that never reached a worker, or a
`route_reason` that will not parse. The consumer counts skips so the gap stays
visible.

**What it does not invent.** A `Subtask` carries no token estimates, no
`allow_external`, and no redaction verdict, so those keep `NormalizedState`'s
defaults rather than being derived from adjacent values — a fabricated input
would make a shadow record compare two decisions that never saw the same state.
`allow_paid` is filled because it is a fact: a recorded `approved_max_cost_usd`
means a human approved spend. `subtask.instructions` — the only free text on the
record — is never read at all; `SHADOW_STATE_KEYS` is the backstop, not the plan.

## Which router you are actually shadowing

This repository has **two** routers, and only one of them is on the bus.

| | worker router | provider router |
|---|---|---|
| module | `core/routing.py` (Engine A) | `router/routing.py` (Engine B) |
| output | `RouteExplanation` | `RoutingDecision` |
| vocabulary | `WorkerLocation`, `PrivacyTier` | `ProviderPrivacyTier`, `PrivacyClass` |
| on the bus? | **yes** — `subtask.routed`, `compute.placed` | **no** |

The decision shadow mode would most like to compare against is
`RoutingDecision`, and it is **not observable today** — the Engine B bridge
carries only `state.changed` ticket rows. So `WORKER_LOCATION_TO_PROVIDER_TIER`
bridges what *is* observable:

| `WorkerLocation` | provider tier | consequence |
|---|---|---|
| `local` | `local_only` | compares cleanly |
| `cloud` | `external` | agrees with either cost tier — see below |
| `rented` | `private_rented` | always `unrepresentable` |

`cloud → external` is safe rather than merely convenient: a `WorkerLocation`
carries no cost signal, and every `cloud_*` capability class admits **both**
`external` and `external_paid`, so the ambiguity cancels instead of
manufacturing a disagreement. It does mean a recorded `provider_tier` of
`external` means "some cloud", not "a free tier".

An unknown or missing location yields `provider_tier=None` and an *uncompared*
record — refusal over guessing, the same rule the projection module follows.

## Reading the metric

`summarize()` returns counts plus `agreement_rate`, whose denominator is stated
rather than assumed:

```
agreement_rate = agree / (agree + disagree)
```

`unrepresentable` and `not_a_provider_route` are **excluded**, not counted as
misses. A rented-node route the control vocabulary cannot name says nothing
about the model's judgement, and folding it in would understate a model that
never had the chance to be right. `agreement_rate` is `None` — never `0.0` —
when nothing was comparable, because `0.0` reads as total disagreement.

Watch, per the model's handoff: agreement, **policy breaches (must stay 0)**,
escalation recall, decision latency, output boundedness. Gate escalation on the
`escalate` flag and `reason_codes`, **never on `confidence`** — the calibration
report puts it at 0.8–1.0 with low resolution, so a threshold on it is noise
wearing a number. `ShadowDecision.should_escalate` reads the flag only.

## What it deliberately does not do

- **No Decision Record.** ADR 0010 §3 keeps control-model output out of the
  governed path. The sink is not the ledger, and the Kernel never reads it —
  structural, not a rule someone must remember.
- **No fallback decision.** On a model failure the "deterministic fallback" is
  the recorded gap. Shadow mode has no decision to fall back to because it was
  never making one.
- **No retry loop.** Exactly one repair attempt on a schema violation, carrying
  the rejection text, then the gap is recorded.
- **No wedging.** An unresolvable event is skipped and the cursor advances; a
  wedged evaluator is worse than a hole in an evaluation dataset. Skips are
  counted so the hole is visible. A *bus read* failure is the one case that
  holds the cursor — nothing was seen, so nothing may be skipped past.
- **No inline reach.** No module under `packages/` may name `control_shadow`
  except shadow mode's own (`SHADOW_MODE_MODULES` in the test). Adding a name
  there is deliberate and reviewable; an accidental import from the live path
  fails CI.

## Where this goes next

The oracle-derived training data means the model *matches* the deterministic
router rather than beating it; the honest expected value is on ambiguous cases
the rules cannot express, which dataset v1.2 does not contain. The shadow
records are dataset v2: `(state, model_decision, router_decision, outcome)` is
exactly the "scrubbed real traces + outcome-derived preference pairs" the model's
own next-iteration plan calls for, and the same outcome stream
`Evaluator.provider_eval_aggregate()` already collects for `learned_quality`.
