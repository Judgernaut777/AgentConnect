"""Test-runner tool: run the operator-configured test command and report a
structured result the model can act on.

The command comes from RuntimeConfig only — the model cannot pass arguments,
which is why this tool is gated independently of the shell (enforced in the
graph). When the command invokes pytest, a --junitxml report is written to a
temp file *outside* the workspace (never visible to the model, always deleted)
and summarized to counts plus failing test names; any other runner — or a
pytest run that produced no report — degrades to the exit code plus the tail
of the raw output, because for logs the tail carries the signal.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import tempfile
import xml.etree.ElementTree as ET

from ..workspace import Workspace
from .shell import CommandTimeout, run_process

_MAX_FAILING_NAMES = 20


def _summarize_junit(path: str) -> tuple[dict[str, int], list[str]] | None:
    """Sum counts across all <testsuite> elements (root may be <testsuites> or
    a bare <testsuite>) and collect failing test names. None if the report is
    missing or unparseable — callers fall back to the raw output tail."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failing: list[str] = []
    for suite in suites:
        for key in counts:
            counts[key] += int(suite.get(key) or 0)
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failing.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    return counts, failing


def run_tests(ws: Workspace, command: str, timeout: float = 300.0, tail_chars: int = 2000) -> str:
    structured = "pytest" in command
    report_path = None
    if structured:
        fd, report_path = tempfile.mkstemp(prefix="agentconnect-junit-", suffix=".xml")
        os.close(fd)
        full = f"{command} --junitxml={shlex.quote(report_path)}"
    else:
        full = command
    try:
        try:
            rc, stdout, stderr = run_process(ws, full, timeout)
        except CommandTimeout:
            # Report the operator's command, never `full` — the temp report
            # path must not leak into the transcript.
            return f"ERROR: test command timed out after {timeout:.0f}s: {command}"
        if structured:
            summary = _summarize_junit(report_path)
            if summary is not None:
                return _format_structured(rc, *summary)
        return _format_tail(rc, stdout, stderr, tail_chars)
    finally:
        if report_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(report_path)


def _format_structured(rc: int, counts: dict[str, int], failing: list[str]) -> str:
    passed = max(counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"], 0)
    lines = [
        f"exit_code={rc}",
        f"tests: passed={passed} failed={counts['failures']} "
        f"errors={counts['errors']} skipped={counts['skipped']}",
    ]
    if counts["failures"] + counts["errors"] > 0:
        lines.append("failing:")
        lines.extend(failing[:_MAX_FAILING_NAMES])
        extra = len(failing) - _MAX_FAILING_NAMES
        if extra > 0:
            lines.append(f"... and {extra} more failing tests")
    return "\n".join(lines)


def _format_tail(rc: int, stdout: str, stderr: str, tail_chars: int) -> str:
    output = stdout + (f"\n{stderr}" if stderr else "")
    tail = output[-tail_chars:] if output else "(no output)"
    return (
        f"exit_code={rc}\n"
        f"tests: no structured results (non-pytest runner or missing report); output tail:\n"
        f"{tail}"
    )
