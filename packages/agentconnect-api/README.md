# agentconnect-api

Adapter package for the AgentConnect backplane. See `docs/BACKPLANE_SPEC.md`.

## `POST /route/decide` — decision-only routing (BrainConnect Lane 4)

The BrainConnect consumer endpoint. It exposes the router's deterministic
`RoutingEngine.route` as a **side-effect-free** query: BrainConnect's
`HttpRoutingClient` POSTs a routing context and gets back a bare `RoutingDecision`.
Unlike `submit_task`/`enqueue_task` it dispatches nothing, enqueues nothing, and
persists nothing (no task, no subtask, no recorded routing decision). It
authenticates like every other route (bearer token; declared action `decide_route`)
and returns 503 if the deployment has no router package/config.

Request body (mirrors `RoutingContext`): `task_id`, `privacy_class`,
`needed_capabilities[]`, `profile`, `require_exact_model`, `est_input_tokens`,
`est_output_tokens`, `allow_external`, `allow_paid`, `priority`, `quality`,
`cloud_safe`, `pending_same_model_batch`, `allow_rented`.

Response (`RoutingDecision`): `task_id`, `decision`, `selected_provider`,
`selected_model`, `rejected_options[]`, `scores[]`, `policy_version`.

See `routes_route.py` and `tests/test_route_decide.py`.
