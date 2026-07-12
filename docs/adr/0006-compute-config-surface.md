# ADR 0006 — Declarative config surface for the external compute plane

Status: accepted (2026-07-12)

## Context

AgentConnect already defines the contract for an external local-model manager
(`core/local_compute.py`: `HttpLocalComputeProvider`, `LocalModelManagerWorkerAdapter`,
the six `LocalComputeProvider` routes). But adopting it required **code**: a caller had
to construct the provider and adapter and pass them into the worker list. Every other
optional subsystem — the whole memory stack — is declared in ENV/YAML and wired by
`bootstrap.memory_from_env`. ComputeConnect's production review recorded this asymmetry
as a cross-repo LOW finding (docs/AGENTCONNECT_INTEGRATION.md, Part IX item 7:
"AgentConnect config does not require programmatic wiring"). The consumer change had to
live here — a sibling repo cannot edit AgentConnect — and ComputeConnect already ships
the server side of the contract, unchanged.

## Decision

Add `compute_worker_from_env()` to `core/bootstrap.py`, following `memory_from_env`
exactly:

- Precedence, byte-for-byte with memory: `AGENTCONNECT_COMPUTE_URL` (env) →
  `compute.base_url` in `config/compute.yaml` → unset (the `local_model_manager` worker
  stays unregistered, which is today's standalone behaviour).
- A **malformed** `compute:` block degrades to off with a warning — a missing compute
  plane is a smaller problem than a wrong one, the same rule memory uses (and the
  opposite of safety, which raises).
- The result, when not `None`, is a
  `LocalModelManagerWorkerAdapter(HttpLocalComputeProvider(url, timeout=…, token=…))`.
  Both classes already existed; only the wiring from config was missing. No engine,
  runtime, or quantization choice is made here — AgentConnect still only asks "can local
  compute handle this?".
- `service_from_env` appends the non-`None` result to the worker list.
- Optional forward-compat `AGENTCONNECT_COMPUTE_TOKEN` / `compute.token` is sent verbatim
  as the `Authorization` header and never logged, mirroring `WIKIBRAIN_TOKEN`. This
  required a small additive `token=` parameter on `HttpLocalComputeProvider`
  (default `None`, backward compatible).
- Optional adapter knobs (`worker_id`, `task_type`, `max_output_tokens`) are read from the
  yaml block; each has a safe default.

The shipped `config/compute.yaml.example` template documents the shape; it is not
auto-loaded — copy it to `config/compute.yaml` (or point
`AGENTCONNECT_COMPUTE_CONFIG` at it) to activate.

## Consequences

- A ComputeConnect deployment is attached with configuration, not code, closing the
  finding. `pip install` + two env vars is enough.
- Standalone AgentConnect is unchanged: with nothing configured there is no compute
  worker, and the malformed-config path can only *remove* the worker, never crash.
- The engine boundary is preserved: AgentConnect owns the contract and the config, not the
  compute engine.

Regression tests: `tests/test_compute_bootstrap.py` — env wiring builds the worker;
absent config → none; malformed yaml → off; yaml base_url + knobs honored; env overrides
yaml; `enabled: false` → off; `service_from_env` appends the worker (and leaves it out
when unconfigured).
