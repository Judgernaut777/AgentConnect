"""The LangGraph execution graph: act -> tool -> act ... -> finalize.

* ``act``      — send the transcript to the model, parse its reply into an Action.
* ``tool``     — execute the action in the workspace, append an OBSERVATION message.
* ``finalize`` — fold the finish action (or the max-steps cutoff) into result fields.

The graph enforces worker-local policy only (step limit, shell/tests/browser
gates, workspace confinement). Global policy — privacy, budget, provider
selection — stays in the router.
"""

from __future__ import annotations

from typing import Any

from agentconnect.common.schemas import GenerateRequest

from langgraph.graph import END, START, StateGraph

from .actions import parse_action
from .agent import ModelSource, RuntimeConfig
from .state import RuntimeState
from .tools import fetch_url, list_dir, read_file, run_shell, run_tests, write_file
from .tools.browser import Fetcher, Resolver
from .workspace import Workspace


def build_execution_graph(
    config: RuntimeConfig,
    model_source: ModelSource,
    workspace: Workspace,
    *,
    fetcher: Fetcher | None = None,
    url_resolver: Resolver | None = None,
) -> Any:
    """Build and compile the worker graph bound to one workspace."""

    def act(state: RuntimeState) -> dict[str, Any]:
        req = GenerateRequest(
            request_id=f"req_{state['task_id']}_{state['iteration']}",
            task_id=state["task_id"],
            model_id=config.model_id,
            messages=state["messages"],
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
        )
        resp = model_source.generate(req)
        action = parse_action(resp.output_text)
        return {
            "messages": state["messages"] + [{"role": "assistant", "content": resp.output_text}],
            "last_action": {"kind": action.kind, "args": action.args, "freeform": action.freeform},
        }

    def run_tool(state: RuntimeState) -> dict[str, Any]:
        action = state["last_action"] or {}
        kind, args = action.get("kind"), action.get("args", {})
        evidence = state["evidence_refs"]
        if kind == "read_file":
            obs = read_file(workspace, args["path"], max_chars=config.observation_max_chars)
            if not obs.startswith("ERROR:"):
                evidence = evidence + [f"read_file:{args['path']}"]
        elif kind == "write_file":
            obs = write_file(workspace, args["path"], args["content"])
        elif kind == "list_dir":
            obs = list_dir(workspace, args.get("path", "."))
        elif kind == "shell":
            if config.allow_shell:
                obs = run_shell(workspace, args["command"], timeout=config.shell_timeout_seconds)
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"shell:{args['command'][:120]}"]
            else:
                obs = "ERROR: the shell action is disabled for this task."
        elif kind == "run_tests":
            # args are deliberately ignored: the command is operator config,
            # never model input. But run_tests still executes workspace code —
            # `pytest` imports every test_*.py under the root and write_file is
            # ungated, so the model can drop a test file whose module-level code
            # runs on import. With no OS sandbox on this worker, allow_shell is
            # the only isolation boundary; run_tests is an equivalent
            # code-execution primitive and must honour it, or allow_shell=False
            # is silently defeated.
            if config.allow_tests and config.allow_shell:
                obs = run_tests(workspace, config.test_command, timeout=config.test_timeout_seconds)
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"run_tests:{config.test_command[:120]}"]
            else:
                obs = "ERROR: the run_tests action is disabled for this task."
        elif kind == "fetch_url":
            if config.allow_browser:
                obs = fetch_url(
                    args["url"],
                    timeout=config.browser_timeout_seconds,
                    max_bytes=config.browser_max_response_bytes,
                    max_redirects=config.browser_max_redirects,
                    fetcher=fetcher,
                    resolver=url_resolver,
                )
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"fetch_url:{args['url'][:120]}"]
            else:
                obs = "ERROR: the browser action is disabled for this task."
        else:  # "invalid"
            obs = f"ERROR: {args.get('error', 'invalid action')} — reply with one valid JSON action."
        if len(obs) > config.observation_max_chars:
            obs = obs[: config.observation_max_chars] + "\n[observation truncated]"
        return {
            "messages": state["messages"] + [{"role": "user", "content": f"OBSERVATION:\n{obs}"}],
            "iteration": state["iteration"] + 1,
            "changed_artifacts": list(workspace.changed_files),
            "evidence_refs": evidence,
        }

    def finalize(state: RuntimeState) -> dict[str, Any]:
        action = state.get("last_action") or {}
        args = action.get("args", {})
        if action.get("kind") == "finish":
            # The finish payload is model output: coerce every field rather than
            # crash the run on a shape deviation (string risks, list next-action,
            # numeric-string confidence, ...).
            try:
                confidence = min(max(float(args.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                confidence = 0.0
            raw_risks = args.get("risks") or []
            if isinstance(raw_risks, str):
                raw_risks = [raw_risks]
            elif not isinstance(raw_risks, (list, tuple)):
                raw_risks = [raw_risks]
            next_action = args.get("recommended_next_action")
            return {
                "done": True,
                "status": "completed",
                "summary": str(args.get("summary", "")),
                "confidence": confidence,
                "risks": state["risks"] + [str(r) for r in raw_risks if r],
                "recommended_next_action": str(next_action) if next_action is not None else None,
                "changed_artifacts": list(workspace.changed_files),
            }
        return {
            "done": False,
            "status": "incomplete",
            "summary": f"Stopped after {state['iteration']} steps without a finish action.",
            "confidence": 0.0,
            "risks": state["risks"] + ["max_steps_reached_before_finish"],
            "recommended_next_action": "Retry with a higher step limit or a narrower task.",
            "changed_artifacts": list(workspace.changed_files),
        }

    def route_after_act(state: RuntimeState) -> str:
        return "finalize" if (state["last_action"] or {}).get("kind") == "finish" else "tool"

    def route_after_tool(state: RuntimeState) -> str:
        return "finalize" if state["iteration"] >= config.max_steps else "act"

    graph = StateGraph(RuntimeState)
    graph.add_node("act", act)
    graph.add_node("tool", run_tool)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {"tool": "tool", "finalize": "finalize"})
    graph.add_conditional_edges("tool", route_after_tool, {"act": "act", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile()
