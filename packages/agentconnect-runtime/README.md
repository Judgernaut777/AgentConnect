# agentconnect-runtime

The AgentConnect worker runtime: the execution layer behind the router.

A LangGraph act/tool loop (`graph.py`) executes one task inside a confined
workspace using filesystem, shell, test-runner, and browser tools, then returns the shared
`WorkerResult` contract. Plain-text models drive tools through a one-JSON-
object-per-turn action protocol (`actions.py`); prose replies resolve as a
free-form finish, so non-protocol models still complete.

The model is reached through the `ModelSource` protocol —
`generate(GenerateRequest) -> GenerateResponse` — satisfied by the
model-manager backends (stub or real), `ResidencyManager`, and the router's
`LocalClient` implementations.

Worker-local policy lives in `RuntimeConfig`: step limit, shell gate,
observation truncation, workspace root. Global policy (privacy, budget,
provider selection) stays in the router.

Remote transport lives in `transport.py`: `create_worker_app` serves the
runtime over HTTP (`POST /run`, `GET /can_accept`; mutual TLS terminates at
the server launcher) and `HttpAgentRuntime` is the matching client. The HTTP
dependencies are optional: `pip install agentconnect-runtime[remote]`.

See the repository-level [docs/AGENT_RUNTIME.md](../../docs/AGENT_RUNTIME.md).
