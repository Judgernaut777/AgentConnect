"""Top-level runtime entrypoint: LangGraph worker loop behind the router.

The runtime depends on a *model source* — anything with
``generate(GenerateRequest) -> GenerateResponse``. The model-manager backends
(`StubBackend`, real vLLM/llama.cpp), `ResidencyManager`, and the router's
`LocalClient` implementations all satisfy it, so the same loop runs against an
in-process stub in tests and a real serving backend in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission, WorkerResult

from .results import worker_result_from_state
from .state import RuntimeState
from .tools.browser import Fetcher, Resolver
from .workspace import Workspace

if TYPE_CHECKING:
    from .memory import MemorySink


class ModelSource(Protocol):
    def generate(self, req: GenerateRequest) -> GenerateResponse: ...


@dataclass(frozen=True)
class RuntimeConfig:
    workspace_root: str = ""  # empty -> fresh temp dir per task
    model_id: str = "qwen3.6-35b-a3b"
    max_steps: int = 12
    max_output_tokens: int = 800
    temperature: float = 0.2
    allow_shell: bool = True
    allow_browser: bool = False
    shell_timeout_seconds: float = 60.0
    # Tests default-on (unlike the browser): the command below is operator
    # config only — the model cannot pass arguments to run_tests. But running
    # the suite still imports (and thus executes) model-written test files, so
    # the graph additionally requires allow_shell before run_tests fires: on a
    # worker with no OS sandbox, allow_shell=False must deny ALL arbitrary code
    # execution, and run_tests is one such path.
    allow_tests: bool = True
    # "python -m pytest" resolves inside any venv/conda env; a wrong command
    # fails loudly (exit 127 + raw output tail), never silently.
    test_command: str = "python -m pytest"
    # Suites legitimately outlast the 60s shell default.
    test_timeout_seconds: float = 300.0
    # Browser default-off (allow_browser above): fetch_url is network egress,
    # unlike every other tool. The timeout is per redirect hop, so a run is
    # bounded by (browser_max_redirects + 1) * browser_timeout_seconds.
    browser_timeout_seconds: float = 20.0
    browser_max_response_bytes: int = 1_000_000  # body cap per response
    browser_max_redirects: int = 5
    # Tool output shown to the model is truncated to this many chars.
    observation_max_chars: int = 4000
    agent_profile: str = "resident_ok"
    # Outbound memory (write-only): default off, like the browser. When on AND a
    # memory_sink is injected, the `remember` action writes durable findings to
    # shared memory (e.g. WikiBrain). The worker never reads memory back.
    allow_memory: bool = False


class AgentRuntime(Protocol):
    def run(self, task: TaskSubmission, task_id: str = "task_local") -> WorkerResult:
        """Execute a task and return the worker contract."""


class LangGraphAgentRuntime:
    """Executes one task at a time through the LangGraph act/tool loop."""

    def __init__(
        self,
        model_source: ModelSource,
        config: RuntimeConfig | None = None,
        *,
        fetcher: Fetcher | None = None,
        url_resolver: Resolver | None = None,
        memory_sink: "MemorySink | None" = None,
    ):
        self.model_source = model_source
        self.config = config or RuntimeConfig()
        # Injection seams for the browser tool (offline tests, custom egress
        # policy); None means the tool's stdlib defaults. Seams are wiring, so
        # they live here rather than in the frozen RuntimeConfig (data only).
        self._fetcher = fetcher
        self._url_resolver = url_resolver
        # Outbound memory seam (write-only). None + allow_memory disables the
        # remember action; a sink is what makes it live.
        self._memory_sink = memory_sink

    def run(self, task: TaskSubmission, task_id: str = "task_local") -> WorkerResult:
        from .graph import build_execution_graph
        from .prompts import build_system_prompt

        workspace = Workspace.create(self.config.workspace_root or None, task_id=task_id)
        try:
            # Provenance for any captured memory: what the manager needs to judge a
            # finding's sensitivity at recall time. privacy_class is the DECLARED one
            # (the router's classified value never reaches the worker).
            provenance = {
                "agent_type": task.agent_type,
                "privacy_class": getattr(task.constraints, "privacy_class", None),
            }
            graph = build_execution_graph(
                self.config,
                self.model_source,
                workspace,
                fetcher=self._fetcher,
                url_resolver=self._url_resolver,
                memory_sink=self._memory_sink,
                provenance=provenance,
            )
            initial: RuntimeState = {
                "task_id": task_id,
                "messages": [
                    {"role": "system", "content": build_system_prompt(task, self.config)},
                    {"role": "user", "content": task.task},
                ],
                "iteration": 0,
                "changed_artifacts": [],
                "evidence_refs": [],
                "risks": [],
                "input_tokens": 0,
                "output_tokens": 0,
            }
            # Each step is an act node plus at most one tool node; headroom covers
            # the finalize node and the entry edge.
            recursion_limit = self.config.max_steps * 2 + 8
            final = graph.invoke(initial, config={"recursion_limit": recursion_limit})
            return worker_result_from_state(final)
        finally:
            # Runtime-created temp workspaces are unreachable by the caller (the
            # contract carries relative paths only), so remove them; a
            # caller-supplied workspace_root is left untouched.
            workspace.cleanup()
