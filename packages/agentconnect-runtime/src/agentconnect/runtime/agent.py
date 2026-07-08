"""Top-level runtime entrypoint: LangGraph worker loop behind the router.

The runtime depends on a *model source* — anything with
``generate(GenerateRequest) -> GenerateResponse``. The model-manager backends
(`StubBackend`, real vLLM/llama.cpp), `ResidencyManager`, and the router's
`LocalClient` implementations all satisfy it, so the same loop runs against an
in-process stub in tests and a real serving backend in production.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission, WorkerResult

from .results import worker_result_from_state
from .state import RuntimeState
from .tools.browser import Fetcher, Resolver
from .workspace import Workspace

if TYPE_CHECKING:
    from .memory import MemorySink


def _safe_dirname(task_id: str) -> str:
    """A filesystem-safe directory name for a task's durable checkpoint dir. Task
    ids are normally simple tokens; this just fences off separators / oddities so a
    crafted id can't escape checkpoint_root."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", task_id or "task")
    return cleaned or "task"


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
    # Hierarchical delegation (Track 4): default off. When on, the `delegate` action
    # lets a planner/manager agent emit sub-tasks (recorded on WorkerResult.subtasks;
    # the router runs them as children then synthesizes). Bounded to prevent runaway
    # recursion: delegation is disabled once delegation_depth reaches
    # max_delegation_depth, and at most max_subtasks may be delegated per run.
    allow_delegation: bool = False
    delegation_depth: int = 0
    max_delegation_depth: int = 2
    max_subtasks: int = 8
    # Mid-run resumability: default off (empty -> today's ephemeral behavior). When
    # set, the run is durable — full state is checkpointed after each super-step
    # under <checkpoint_root>/<task_id>/ and the workspace lives there too (not a
    # throwaway temp), so a crashed run RE-DISPATCHED with the same task_id resumes
    # mid-reasoning instead of restarting. The whole dir is removed on completion.
    checkpoint_root: str = ""


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

        # Resumable mode (checkpoint_root set): a durable LangGraph SqliteSaver holds
        # the graph state and the workspace lives beside it, both keyed by task_id, so
        # a re-dispatched run reattaches to both and resumes from the pending node.
        # Otherwise: today's ephemeral temp workspace, no checkpointer, in-memory.
        base_dir: Path | None = None
        conn = None
        checkpointer = None
        if self.config.checkpoint_root:
            base_dir = Path(self.config.checkpoint_root) / _safe_dirname(task_id)
            base_dir.mkdir(parents=True, exist_ok=True)
            conn, checkpointer = self._open_checkpointer(base_dir / "checkpoint.sqlite")
            workspace_root: str | None = str(base_dir / "workspace")
        else:
            workspace_root = self.config.workspace_root or None

        workspace = Workspace.create(workspace_root, task_id=task_id)
        success = False
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
                checkpointer=checkpointer,
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
                "subtasks": [],
            }
            # Each step is an act node plus at most one tool node; headroom covers
            # the finalize node and the entry edge.
            recursion_limit = self.config.max_steps * 2 + 8
            if checkpointer is None:
                final = graph.invoke(initial, config={"recursion_limit": recursion_limit})
            else:
                cfg = {
                    "configurable": {"thread_id": task_id},
                    "recursion_limit": recursion_limit,
                }
                prior = graph.get_state(cfg)
                if prior.next:
                    # A prior run left pending work: resume from the exact pending
                    # node (prior nodes are NOT re-run). Prime the workspace's
                    # changed-file tracker from the checkpoint — the fresh Workspace
                    # object starts empty and each tool node overwrites
                    # changed_artifacts with workspace.changed_files, so without this
                    # pre-crash file changes would drop out of the final result.
                    workspace.changed_files = list(prior.values.get("changed_artifacts", []))
                    final = graph.invoke(None, config=cfg)
                else:
                    final = graph.invoke(initial, config=cfg)
            result = worker_result_from_state(final)
            success = True
            return result
        finally:
            if conn is not None:
                conn.close()
            if checkpointer is None:
                # Runtime-created temp workspaces are unreachable by the caller (the
                # contract carries relative paths only), so remove them; a
                # caller-supplied workspace_root is left untouched.
                workspace.cleanup()
            elif success and base_dir is not None:
                # Completed: drop the whole durable dir (workspace + checkpoint.sqlite).
                # On a hard crash the finally is skipped, so both survive for resume; if
                # finally DOES run on a propagating error we also keep them (success is
                # False), so a re-dispatch can resume.
                shutil.rmtree(base_dir, ignore_errors=True)

    def _open_checkpointer(self, db_path: Path):
        """Open a durable SQLite checkpointer at ``db_path``. Returns (conn, saver);
        the caller closes ``conn`` in its finally. Behind the ``[resumable]`` extra."""
        import sqlite3

        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "mid-run resumability (RuntimeConfig.checkpoint_root) needs the "
                "[resumable] extra: pip install 'agentconnect-runtime[resumable]'"
            ) from e
        # check_same_thread=False: the runtime may touch the saver from a helper
        # thread; a single run is otherwise single-threaded through the graph.
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        return conn, saver
