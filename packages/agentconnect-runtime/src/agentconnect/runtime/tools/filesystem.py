"""Filesystem tools: read, write, and list inside the task workspace.

Each function returns an observation string for the model. Errors (missing
files, escaping paths) come back as ``ERROR: ...`` observations rather than
exceptions, so the loop can show them to the model and continue.
"""

from __future__ import annotations

from ..workspace import Workspace, WorkspaceError


def read_file(ws: Workspace, path: str) -> str:
    try:
        target = ws.resolve(path)
        if not target.is_file():
            return f"ERROR: file not found: {path}"
        return target.read_text(encoding="utf-8", errors="replace")
    except WorkspaceError as exc:
        return f"ERROR: {exc}"


def write_file(ws: Workspace, path: str, content: str) -> str:
    try:
        target = ws.resolve(path)
    except WorkspaceError as exc:
        return f"ERROR: {exc}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    ws.record_change(path)
    return f"OK: wrote {len(content)} chars to {path}"


def list_dir(ws: Workspace, path: str = ".") -> str:
    try:
        target = ws.resolve(path or ".")
    except WorkspaceError as exc:
        return f"ERROR: {exc}"
    if not target.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(e.name + ("/" if e.is_dir() else "") for e in target.iterdir())
    return "\n".join(entries) if entries else "(empty)"
