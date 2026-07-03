# agentconnect-runtime

The AgentConnect worker runtime: the execution layer behind the router.

A LangGraph act/tool loop (`graph.py`) executes one task inside a confined
workspace using filesystem and shell tools, then returns the shared
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

Not yet implemented: browser tool, dedicated test-runner tool, remote
transport (`transport.py` is a stub).

See the repository-level [docs/AGENT_RUNTIME.md](../../docs/AGENT_RUNTIME.md).
