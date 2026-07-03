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
{shell_action}{{"action": "finish", "summary": "<what you did / found>", "confidence": <0.0-1.0>, "risks": ["<risk>", ...], "recommended_next_action": "<optional>"}}

Rules:
- Paths must stay inside the workspace; there is no access outside it.
- After each action you receive an OBSERVATION message with the result.
- When the task is complete (or impossible), emit the finish action.
- You have at most {max_steps} actions before the run is cut off.
"""

_SHELL_LINE = '{"action": "shell", "command": "<command run in the workspace>"}\n'


def build_system_prompt(task: TaskSubmission, config: RuntimeConfig) -> str:
    protocol = _PROTOCOL.format(
        shell_action=_SHELL_LINE if config.allow_shell else "",
        max_steps=config.max_steps,
    )
    return f"{protocol}\nProfile: {config.agent_profile}\nTask:\n{task.task}"
