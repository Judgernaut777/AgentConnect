# Review Findings

> **Status note (2026-07-24).** A historical snapshot from the pre-rename,
> 89-test-era review; the numbered items mirror `REVIEW.md`. Current status:
> **1 fixed** (cloud calls go through LiteLLM — `_call_via_litellm` in
> `gateway.py`, native handlers via `ProviderConfig.litellm_model`, pinned by
> `tests/test_gateway_cloud.py`); **2 still true and deliberate** (the stub
> fallback is documented and test-pinned); **3 fixed** (pre-storage clamp
> removed; regression `tests/test_artifact_full_storage.py`); **4 fixed**
> (`time.time()` passed at all four `pool.acquire`/`pool.release` sites;
> regression `tests/test_nodepool_concurrency.py::test_idle_reaper_spares_recently_used_node`);
> **5 still true** (per-process quota reservations, documented in
> `docs/MULTI_HARNESS.md`); **6 largely addressed** (`docs/STATUS.md` and the
> README trust-boundary section).

This note captures the main issues found in the current repository review.

## Findings

1. `packages/agentconnect-router/src/agentconnect/router/gateway.py` treats all cloud providers as OpenAI-compatible and always posts to `/chat/completions`. That is fine for OpenAI/Groq-compatible endpoints, but it will not work for providers that require a different request/response shape, which is why provider-specific adapters are needed.

2. `packages/agentconnect-router/src/agentconnect/router/gateway.py` silently falls back to a deterministic stub when a cloud secret is missing or a cloud request fails. That keeps demos working, but in production it can make a failed cloud dispatch look like success.

3. `packages/agentconnect-router/src/agentconnect/router/service.py` clamps the output before writing the artifact to shared memory. That means the stored artifact is not actually the full output, which weakens the “read large outputs back in chunks” story.

4. `packages/agentconnect-router/src/agentconnect/router/provisioning.py` stores rented-node timestamps with a default of `0.0`, and `packages/agentconnect-router/src/agentconnect/router/service.py` calls the pool without passing a real time value. That can make warm rented nodes look stale when idle reaping runs.

5. `packages/agentconnect-core/src/agentconnect/common/quota.py` keeps live reservations in process memory only. If more than one router process runs against the same shared memory database, quota oversubscription becomes possible.

6. `README.md` and `docs/ARCHITECTURE.md` overstate maturity relative to the implementation in a few places. The stack is solid, but it is still closer to a structured prototype than a production-ready control plane.

## Verification

The test suite passed in the local sandbox with the then-installed dependencies
(historical — see the changelog for the current gate):

`89 passed, 1 warning`
