"""Shell tool: run a command inside the task workspace.

Availability is gated by ``RuntimeConfig.allow_shell`` (enforced in the graph,
not here). Output is combined stdout+stderr plus the exit code, formatted as an
observation string for the model.
"""

from __future__ import annotations

import subprocess

from ..workspace import Workspace


def run_shell(ws: Workspace, command: str, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout:.0f}s: {command}"
    parts = [f"exit_code={proc.returncode}"]
    if proc.stdout:
        parts.append(f"stdout:\n{proc.stdout}")
    if proc.stderr:
        parts.append(f"stderr:\n{proc.stderr}")
    return "\n".join(parts)
