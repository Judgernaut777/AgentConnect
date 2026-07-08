"""Prompt assembly for the worker runtime.

The system prompt defines the action protocol (see ``actions.py``): one JSON
object per model turn. It is deliberately compact — local worker models have
small context windows and the observation history grows every step.
"""

from __future__ import annotations

from agentconnect.common.schemas import TaskSubmission

from .agent import RuntimeConfig

_PROTOCOL = """\
You are a worker agent executing one task inside a workspace directory.

Respond with EXACTLY one JSON object per turn and nothing else. Available actions:

{{"action": "read_file", "path": "<path relative to the workspace>"}}
{{"action": "write_file", "path": "<relative path>", "content": "<full new file content>"}}
{{"action": "list_dir", "path": "<relative path, default .>"}}
{shell_action}{tests_action}{browser_action}{memory_action}{delegate_action}{{"action": "finish", "summary": "<what you did / found>", "confidence": <0.0-1.0>, "risks": ["<risk>", ...], "recommended_next_action": "<optional>"}}

Rules:
- Paths must stay inside the workspace; there is no access outside it.
- After each action you receive an OBSERVATION message with the result.
- When the task is complete (or impossible), emit the finish action.
- You have at most {max_steps} actions before the run is cut off.
"""

_SHELL_LINE = '{"action": "shell", "command": "<command run in the workspace>"}\n'
_TESTS_LINE = '{"action": "run_tests"}  (runs the project\'s test suite)\n'
_BROWSER_LINE = '{"action": "fetch_url", "url": "<http(s) URL, returned as readable text>"}\n'
_MEMORY_LINE = (
    '{"action": "remember", "text": "<a durable finding worth keeping>"}'
    "  (writes to shared memory; you cannot read it back)\n"
)
_DELEGATE_LINE = (
    '{"action": "delegate", "task": "<a self-contained sub-task>", "agent_type": "<optional role>"}'
    "  (runs as a child task; you get back only a synthesized summary — keep your own context small)\n"
)


def build_system_prompt(task: TaskSubmission, config: RuntimeConfig) -> str:
    # Advertise `delegate` only while it can actually fire — off at the depth limit,
    # so a leaf agent isn't told to decompose work it must do itself.
    can_delegate = config.allow_delegation and config.delegation_depth < config.max_delegation_depth
    protocol = _PROTOCOL.format(
        shell_action=_SHELL_LINE if config.allow_shell else "",
        # run_tests executes workspace code, so the graph gates it on allow_shell
        # too; only advertise it when it can actually run.
        tests_action=_TESTS_LINE if (config.allow_tests and config.allow_shell) else "",
        browser_action=_BROWSER_LINE if config.allow_browser else "",
        memory_action=_MEMORY_LINE if config.allow_memory else "",
        delegate_action=_DELEGATE_LINE if can_delegate else "",
        max_steps=config.max_steps,
    )
    return f"{protocol}\nProfile: {config.agent_profile}\nTask:\n{task.task}"
