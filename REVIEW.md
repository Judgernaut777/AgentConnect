# Repository Review

> **Status note (2026-07-24).** This is a historical point-in-time review of the
> pre-rename repository (`mcp-agentconnect`, 89-test era). The finding bodies below
> are preserved as written; their current status against the code:
>
> 1. **Fixed** — the gateway now routes cloud calls through LiteLLM
>    (`_call_via_litellm` in `gateway.py`); `ProviderConfig.litellm_model` selects a
>    native provider handler (e.g. `gemini/…`), with OpenAI-compatible mode as the
>    default. Pinned by `tests/test_gateway_cloud.py`.
> 2. **Still true, and deliberate** — the `[cloud-stub:…]` fallback remains,
>    documented in the `_call_cloud` docstring and test-pinned
>    (`tests/test_gateway_cloud.py`); a production build would remove it.
> 3. **Fixed** — the pre-storage clamp is removed; artifacts store the full output
>    and `read_artifact_chunk` pages it back bounded. Regression:
>    `tests/test_artifact_full_storage.py`.
> 4. **Fixed** — the router service passes `time.time()` at all four
>    `pool.acquire`/`pool.release` sites. Regression:
>    `tests/test_nodepool_concurrency.py::test_idle_reaper_spares_recently_used_node`.
> 5. **Still true** — quota reservations are per-process; documented as a known
>    limitation (`docs/MULTI_HARNESS.md`, Model A caveats).
> 6. **Largely addressed** — `docs/STATUS.md` states the stabilization boundary and
>    test-fidelity limits; `README.md` leads with the trust boundary.
>
> The verification counts below predate the current suite; see the changelog for
> the measured gate at head.

Scope: `mcp-agentconnect` as checked out in this workspace.

## Summary

The concept is strong and the code is much more coherent than most first-pass agent-control-plane repos. The repo has a real architecture, good boundaries, and unusually broad offline coverage. The main issue is that a few production-facing claims still outrun the implementation.

## Findings

1. Provider adapter support is missing.
   `packages/agentconnect-router/src/agentconnect/router/gateway.py` treats all cloud providers as OpenAI-compatible and posts to `/chat/completions` for every cloud call. That works for OpenAI-compatible backends, but it will not work for providers with different request and response shapes. Gemini in `config/providers.yaml` is the obvious mismatch, which is why the gateway needs a provider-adapter layer instead of a single cloud call path.

2. Cloud failures degrade to a success-looking stub.
   The same gateway swallows missing secrets and outbound call failures, then returns a deterministic `[cloud-stub:...]` result. That is useful for demoability, but in production it can hide a real integration failure behind what looks like a completed task.

3. Artifact storage is capped too early.
   `packages/agentconnect-router/src/agentconnect/router/service.py` clamps the output before writing the artifact to shared memory. That means the stored artifact is not actually the full output, so the “read detail back in chunks” story is weaker than the docs imply.

4. Rented-node timestamps are effectively uninitialized.
   `packages/agentconnect-router/src/agentconnect/router/provisioning.py` defaults `NodePool.acquire()` and `release()` timestamps to `0.0`, and `packages/agentconnect-router/src/agentconnect/router/service.py` calls them without passing a real timestamp. A later idle reaper can therefore treat warm rented nodes as ancient and terminate them too aggressively.

5. Quota reservations are only safe in one process.
   `packages/agentconnect-core/src/agentconnect/common/quota.py` keeps live reservations in process memory. If multiple router processes run against the same shared store, they can oversubscribe a shared quota because reservations are not persisted or atomically claimed.

6. The docs overstate maturity in a few spots.
   `README.md` and `docs/ARCHITECTURE.md` present the system as fully implemented across all phases, but the code is still closer to a structured prototype than a production-ready control plane. The architecture is credible; the “done” language is ahead of the actual failure modes and adapter coverage.

## Good Parts

The repo has a strong separation of concerns:

- `agentconnect-core` carries contracts, memory, privacy, quota, auth, and config.
- `agentconnect-router` owns routing, policy, spend control, and MCP tools.
- `agentconnect-model-manager` stays narrowly focused on local residency and generation.

The privacy and spend controls are serious work, not decorative. The tests show that `secret_sensitive` blocks, budget gating fail-closes, mTLS works, and the approval UI path is exercised end to end. The offline demo path is also thoughtful.

## Verification

Local verification in this workspace (historical — the suite has since grown to
four figures; see the changelog for the current gate):

- Full suite: `89 passed, 1 warning`
- mTLS tests pass with local socket permission
- approval web tests pass with local runtime permission

## Recommendation

The next substantive step is to add a provider-adapter layer and make the gateway explicit about which providers are real integrations versus demo stubs.
