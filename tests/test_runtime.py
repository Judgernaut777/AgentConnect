"""Agent runtime: LangGraph act/tool loop, action protocol, workspace confinement.

All tests run offline. A ScriptedModelSource plays the model's side of the
action protocol; the last test drives the loop through the model-manager stub
backend to prove the ModelSource seam matches the rest of the system.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.runtime import (
    LangGraphAgentRuntime,
    RuntimeConfig,
    Workspace,
    WorkspaceError,
    parse_action,
)


class ScriptedModelSource:
    """Replays a fixed sequence of model replies; repeats the last one."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[GenerateRequest] = []

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.requests.append(req)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


def _finish(summary: str, **kw) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9, **kw})


# ------------------------------------------------------------ action parsing
def test_parse_action_valid_json():
    a = parse_action('{"action": "read_file", "path": "a.py"}')
    assert a.kind == "read_file" and a.args["path"] == "a.py"


def test_parse_action_tolerates_fences_and_prose():
    a = parse_action('Sure, here you go:\n```json\n{"action": "list_dir", "path": "."}\n```')
    assert a.kind == "list_dir"


def test_parse_action_prose_becomes_freeform_finish():
    a = parse_action("The answer is 42.")
    assert a.kind == "finish" and a.freeform and a.args["summary"] == "The answer is 42."


def test_parse_action_unknown_or_incomplete_is_invalid():
    assert parse_action('{"action": "rm_rf", "path": "/"}').kind == "invalid"
    assert parse_action('{"action": "write_file", "path": "a.py"}').kind == "invalid"


def test_parse_run_tests_action():
    assert parse_action('{"action": "run_tests"}').kind == "run_tests"


# ------------------------------------------------------- workspace confinement
def test_workspace_rejects_escaping_paths(tmp_path):
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.resolve("../outside.txt")
    with pytest.raises(WorkspaceError):
        ws.resolve("a/../../outside.txt")
    assert ws.resolve("sub/inside.txt").is_relative_to(ws.root)


# ------------------------------------------------------------------ the loop
def _runtime(replies: list[str], tmp_path, **cfg) -> tuple[LangGraphAgentRuntime, ScriptedModelSource]:
    source = ScriptedModelSource(replies)
    config = RuntimeConfig(workspace_root=str(tmp_path), **cfg)
    return LangGraphAgentRuntime(source, config), source


def test_write_then_finish(tmp_path):
    rt, source = _runtime(
        [
            json.dumps({"action": "write_file", "path": "hello.txt", "content": "hi\n"}),
            _finish("Wrote hello.txt", risks=["none"], recommended_next_action="review it"),
        ],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="create hello.txt"), task_id="t1")
    assert result.status == "completed"
    assert result.summary == "Wrote hello.txt"
    assert result.confidence == 0.9
    assert result.changed_artifacts == ["hello.txt"]
    assert "none" in result.risks
    assert result.recommended_next_action == "review it"
    assert (tmp_path / "hello.txt").read_text() == "hi\n"
    # act was called once per scripted reply
    assert len(source.requests) == 2


def test_read_feeds_observation_back_to_model(tmp_path):
    (tmp_path / "config.ini").write_text("mode=fast\n")
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "config.ini"}), _finish("mode is fast")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="what mode?"), task_id="t2")
    assert result.status == "completed"
    assert "read_file:config.ini" in result.evidence_refs
    # The second model call saw the file content as an observation.
    second_call = source.requests[1].messages
    assert any("mode=fast" in m["content"] for m in second_call if m["role"] == "user")


def test_shell_runs_in_workspace(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "shell", "command": "pwd"}), _finish("ran it")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="run pwd"), task_id="t3")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    # pwd prints the physical path — the command really ran inside the workspace
    assert str(tmp_path.resolve()) in obs and "exit_code=0" in obs


def test_shell_disabled_by_policy(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "shell", "command": "echo nope"}), _finish("ok")],
        tmp_path,
        allow_shell=False,
    )
    rt.run(TaskSubmission(task="try shell"), task_id="t4")
    obs = source.requests[1].messages[-1]["content"]
    assert "disabled" in obs


def test_escaping_path_becomes_error_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "../../etc/hostname"}), _finish("done")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="escape"), task_id="t5")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "ERROR" in obs and "escapes the workspace" in obs
    # a blocked read is not evidence
    assert result.evidence_refs == []


def test_invalid_action_gets_retry_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "teleport", "to": "prod"}), _finish("recovered")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="be weird"), task_id="t6")
    assert result.status == "completed" and result.summary == "recovered"
    obs = source.requests[1].messages[-1]["content"]
    assert "unknown action" in obs


def test_max_steps_cuts_off_looping_model(tmp_path):
    rt, _ = _runtime(
        [json.dumps({"action": "list_dir", "path": "."})],  # loops forever
        tmp_path,
        max_steps=3,
    )
    result = rt.run(TaskSubmission(task="loop"), task_id="t7")
    assert result.status == "incomplete"
    assert "max_steps_reached_before_finish" in result.risks


def test_prose_reply_is_final_answer(tmp_path):
    rt, _ = _runtime(["Everything looks fine, no changes needed."], tmp_path)
    result = rt.run(TaskSubmission(task="assess"), task_id="t8")
    assert result.status == "completed"
    assert result.summary == "Everything looks fine, no changes needed."


def test_long_observation_truncated(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 10_000)
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "big.txt"}), _finish("read it")],
        tmp_path,
        observation_max_chars=500,
    )
    rt.run(TaskSubmission(task="read big"), task_id="t9")
    obs = source.requests[1].messages[-1]["content"]
    assert "[observation truncated]" in obs and len(obs) < 700


def test_observation_truncation_boundary(tmp_path):
    """A file exactly at the limit passes through verbatim — no spurious marker."""
    (tmp_path / "at.txt").write_text("x" * 500)
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "at.txt"}), _finish("ok")],
        tmp_path,
        observation_max_chars=500,
    )
    rt.run(TaskSubmission(task="read at-limit"), task_id="t9a")
    assert source.requests[1].messages[-1]["content"] == "OBSERVATION:\n" + "x" * 500


# ------------------------------------------- robustness against model mistakes
def test_os_error_becomes_observation_not_crash(tmp_path):
    """Writing over a directory is a realistic model mistake; it must come back
    as an ERROR observation, not abort the run without a WorkerResult."""
    rt, source = _runtime(
        [
            json.dumps({"action": "write_file", "path": "sub/a.txt", "content": "x"}),
            json.dumps({"action": "write_file", "path": "sub", "content": "y"}),
            _finish("recovered"),
        ],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="clumsy writes"), task_id="t12")
    assert result.status == "completed" and result.summary == "recovered"
    obs = source.requests[2].messages[-1]["content"]
    assert "ERROR" in obs
    assert result.changed_artifacts == ["sub/a.txt"]


def test_null_byte_path_becomes_error_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "read_file", "path": "a\x00b"}), _finish("done")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="weird path"), task_id="t13")
    assert result.status == "completed"
    assert "ERROR" in source.requests[1].messages[-1]["content"]


def test_finish_fields_are_coerced_not_crashed(tmp_path):
    """The finish payload is model output: string risks, list next-action, and
    numeric-string confidence must be coerced into the WorkerResult shape."""
    rt, _ = _runtime(
        [
            json.dumps(
                {
                    "action": "finish",
                    "summary": "done",
                    "confidence": "0.9",
                    "risks": "might break",
                    "recommended_next_action": ["review", "deploy"],
                }
            )
        ],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="finish oddly"), task_id="t14")
    assert result.status == "completed"
    assert result.confidence == 0.9
    assert result.risks == ["might break"]
    assert isinstance(result.recommended_next_action, str)


def test_non_numeric_confidence_degrades_to_zero(tmp_path):
    rt, _ = _runtime(
        [json.dumps({"action": "finish", "summary": "done", "confidence": "high"})],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="t"), task_id="t15")
    assert result.status == "completed" and result.confidence == 0.0


def test_action_found_after_incidental_json():
    a = parse_action('Scores: {"tests": 12}\n{"action": "finish", "summary": "all green"}')
    assert a.kind == "finish" and a.args["summary"] == "all green"


def test_empty_content_write_is_valid(tmp_path):
    assert parse_action('{"action": "write_file", "path": "a.py", "content": ""}').kind == "write_file"
    rt, _ = _runtime(
        [json.dumps({"action": "write_file", "path": "empty.txt", "content": ""}), _finish("ok")],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="touch a file"), task_id="t16")
    assert result.status == "completed"
    assert (tmp_path / "empty.txt").exists()
    assert result.changed_artifacts == ["empty.txt"]


def test_evidence_refs_accumulate_across_tool_calls(tmp_path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    rt, _ = _runtime(
        [
            json.dumps({"action": "read_file", "path": "a.txt"}),
            json.dumps({"action": "shell", "command": "echo hi"}),
            json.dumps({"action": "read_file", "path": "b.txt"}),
            _finish("gathered"),
        ],
        tmp_path,
    )
    result = rt.run(TaskSubmission(task="gather"), task_id="t17")
    assert result.evidence_refs == ["read_file:a.txt", "shell:echo hi", "read_file:b.txt"]


def test_shell_timeout_returns_error_observation(tmp_path):
    rt, source = _runtime(
        [json.dumps({"action": "shell", "command": "sleep 5"}), _finish("gave up")],
        tmp_path,
        shell_timeout_seconds=0.5,
    )
    result = rt.run(TaskSubmission(task="slow command"), task_id="t18")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "timed out" in obs
    # a failed command is not evidence
    assert result.evidence_refs == []


def test_default_config_uses_fresh_temp_workspace_and_cleans_up():
    """With workspace_root unset, work must land in a fresh temp dir (never the
    process CWD) and the temp dir must be removed after the run."""
    import os
    import tempfile

    replies = [
        json.dumps({"action": "write_file", "path": "hello.txt", "content": "hi"}),
        _finish("wrote it"),
    ]
    rt = LangGraphAgentRuntime(ScriptedModelSource(replies))  # default RuntimeConfig
    result = rt.run(TaskSubmission(task="write hello"), task_id="tws")
    assert result.status == "completed"
    assert result.changed_artifacts == ["hello.txt"]
    assert not (Path(os.getcwd()) / "hello.txt").exists()
    leftovers = [
        d for d in Path(tempfile.gettempdir()).iterdir()
        if d.name.startswith("agentconnect-ws-tws-")
    ]
    assert leftovers == []


# ------------------------------------------------------------ run_tests tool
# The venv's pytest regardless of PATH; nested runs are local-only subprocesses.
PYTEST_CMD = f"{shlex.quote(sys.executable)} -m pytest -q"

_RUN_TESTS = json.dumps({"action": "run_tests"})


def _seed_suite(tmp_path):
    """One passing + one failing + one skipped test."""
    (tmp_path / "test_demo.py").write_text(
        "import pytest\n"
        "def test_pass():\n"
        "    assert True\n"
        "def test_fail():\n"
        "    assert False\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_skip():\n"
        "    pass\n"
    )


def test_run_tests_reports_structured_counts_and_failing_names(tmp_path):
    _seed_suite(tmp_path)
    rt, source = _runtime([_RUN_TESTS, _finish("ran tests")], tmp_path, test_command=PYTEST_CMD)
    result = rt.run(TaskSubmission(task="run the tests"), task_id="rt1")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "exit_code=1" in obs
    assert "passed=1 failed=1 errors=0 skipped=1" in obs
    assert "failing:" in obs
    assert "test_demo::test_fail" in obs


def test_run_tests_all_green_and_evidence_ref(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    rt, source = _runtime([_RUN_TESTS, _finish("green")], tmp_path, test_command=PYTEST_CMD)
    result = rt.run(TaskSubmission(task="run the tests"), task_id="rt2")
    obs = source.requests[1].messages[-1]["content"]
    assert "exit_code=0" in obs
    assert "passed=1 failed=0" in obs
    assert "failing:" not in obs
    assert f"run_tests:{PYTEST_CMD[:120]}" in result.evidence_refs


def test_run_tests_failing_suite_still_yields_evidence(tmp_path):
    """Red results are evidence too; only ERROR observations are not."""
    _seed_suite(tmp_path)
    rt, _ = _runtime([_RUN_TESTS, _finish("red")], tmp_path, test_command=PYTEST_CMD)
    result = rt.run(TaskSubmission(task="run the tests"), task_id="rt3")
    assert f"run_tests:{PYTEST_CMD[:120]}" in result.evidence_refs


def test_run_tests_non_pytest_runner_falls_back_to_tail(tmp_path):
    rt, source = _runtime(
        [_RUN_TESTS, _finish("ok")], tmp_path, test_command="echo one; echo two; exit 3"
    )
    rt.run(TaskSubmission(task="run the tests"), task_id="rt4")
    obs = source.requests[1].messages[-1]["content"]
    assert "exit_code=3" in obs
    assert "no structured results" in obs
    assert "two" in obs
    # no junit injection for non-pytest commands
    assert "--junitxml" not in obs


def test_run_tests_disabled_by_policy(tmp_path):
    rt, source = _runtime(
        [_RUN_TESTS, _finish("ok")], tmp_path, allow_tests=False, test_command=PYTEST_CMD
    )
    result = rt.run(TaskSubmission(task="run the tests"), task_id="rt5")
    obs = source.requests[1].messages[-1]["content"]
    assert "disabled" in obs
    assert result.evidence_refs == []


def test_run_tests_requires_shell_gate(tmp_path):
    """run_tests imports (and thus executes) workspace test files, so it is an
    arbitrary-code-execution primitive equal to shell. With allow_shell=False
    and no OS sandbox, it must stay disabled even when allow_tests is True —
    otherwise the shell gate is silently defeated."""
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    rt, source = _runtime(
        [_RUN_TESTS, _finish("done")],
        tmp_path,
        allow_shell=False,
        allow_tests=True,
        test_command=PYTEST_CMD,
    )
    result = rt.run(TaskSubmission(task="test without shell"), task_id="rt6")
    assert result.status == "completed"
    tests_obs = source.requests[1].messages[-1]["content"]
    assert "disabled" in tests_obs
    assert result.evidence_refs == []
    # The action is not advertised when it cannot run.
    prompt = source.requests[0].messages[0]["content"]
    assert "run_tests" not in prompt
    assert '"action": "shell"' not in prompt


def test_run_tests_cannot_bypass_shell_gate_via_written_test(tmp_path):
    """Concrete pwn: with allow_shell=False the model writes a test whose
    module-level code executes on pytest import, then calls run_tests. The gate
    must block run_tests so the payload never runs (marker file never created)."""
    marker = tmp_path / "PWNED"
    payload = (
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n"
        "def test_noop():\n    assert True\n"
    )
    rt, source = _runtime(
        [
            json.dumps({"action": "write_file", "path": "test_pwn.py", "content": payload}),
            _RUN_TESTS,
            _finish("attempted"),
        ],
        tmp_path,
        allow_shell=False,
        allow_tests=True,
        test_command=PYTEST_CMD,
    )
    rt.run(TaskSubmission(task="pwn the host"), task_id="rt6b")
    run_tests_obs = source.requests[2].messages[-1]["content"]
    assert "disabled" in run_tests_obs
    assert not marker.exists()  # payload never executed


def test_run_tests_prompt_line_absent_when_tests_disabled(tmp_path):
    """Finding 4: the run_tests template must disappear when allow_tests=False,
    mirroring the shell/browser conditional lines (both directions covered)."""
    rt_on, source_on = _runtime([_finish("done")], tmp_path, allow_tests=True)
    rt_on.run(TaskSubmission(task="anything"), task_id="rt6c")
    assert "run_tests" in source_on.requests[0].messages[0]["content"]

    rt_off, source_off = _runtime([_finish("done")], tmp_path, allow_tests=False)
    rt_off.run(TaskSubmission(task="anything"), task_id="rt6d")
    assert "run_tests" not in source_off.requests[0].messages[0]["content"]


def test_run_tests_timeout_kills_process_group(tmp_path):
    rt, source = _runtime(
        [_RUN_TESTS, _finish("gave up")],
        tmp_path,
        test_command="sleep 5",
        test_timeout_seconds=0.5,
    )
    result = rt.run(TaskSubmission(task="slow suite"), task_id="rt7")
    obs = source.requests[1].messages[-1]["content"]
    assert "timed out" in obs
    assert result.evidence_refs == []


def test_run_tests_leaves_no_report_in_workspace(tmp_path, monkeypatch):
    """The junit report lives in system temp, never the workspace, and is
    always deleted."""
    import tempfile

    # A private tempdir, so a run_tests in flight in some other checkout or CI
    # shard (sharing the system tempdir) cannot flake the leftovers assertion.
    private_tmp = tmp_path / "private-tmp"
    private_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private_tmp))

    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    rt, _ = _runtime([_RUN_TESTS, _finish("clean")], tmp_path, test_command=PYTEST_CMD)
    rt.run(TaskSubmission(task="run the tests"), task_id="rt8")
    assert list(tmp_path.rglob("*.xml")) == []
    leftovers = [
        p for p in private_tmp.iterdir()
        if p.name.startswith("agentconnect-junit-")
    ]
    assert leftovers == []


def test_summarize_junit_handles_wrapped_and_bare_roots(tmp_path):
    from agentconnect.runtime.tools.tests import _summarize_junit

    wrapped = tmp_path / "wrapped.xml"
    wrapped.write_text(
        '<testsuites>'
        '<testsuite tests="2" failures="1" errors="0" skipped="0">'
        '<testcase classname="a" name="ok"/>'
        '<testcase classname="a" name="bad"><failure/></testcase>'
        '</testsuite>'
        '<testsuite tests="1" failures="0" errors="1" skipped="0">'
        '<testcase classname="b" name="boom"><error/></testcase>'
        '</testsuite>'
        '</testsuites>'
    )
    counts, failing = _summarize_junit(str(wrapped))
    assert counts == {"tests": 3, "failures": 1, "errors": 1, "skipped": 0}
    assert failing == ["a::bad", "b::boom"]

    bare = tmp_path / "bare.xml"
    bare.write_text(
        '<testsuite tests="2" failures="1" errors="1" skipped="0">'
        '<testcase classname="a" name="bad"><failure/></testcase>'
        '<testcase classname="b" name="boom"><error/></testcase>'
        '</testsuite>'
    )
    counts, failing = _summarize_junit(str(bare))
    assert counts == {"tests": 2, "failures": 1, "errors": 1, "skipped": 0}
    assert failing == ["a::bad", "b::boom"]

    garbage = tmp_path / "garbage.xml"
    garbage.write_text("not xml at all")
    assert _summarize_junit(str(garbage)) is None


def test_format_structured_caps_failing_names_with_overflow_count(tmp_path):
    """Finding 7: >20 failing tests are truncated to exactly 20 names plus a
    '... and N more failing tests' line. Exercises _summarize_junit's slice and
    _format_structured's overflow arithmetic end-to-end."""
    from agentconnect.runtime.tools.tests import _format_structured, _summarize_junit

    cases = "".join(
        f'<testcase classname="m" name="t{i}"><failure/></testcase>' for i in range(25)
    )
    report = tmp_path / "many.xml"
    report.write_text(f'<testsuite tests="25" failures="25" errors="0" skipped="0">{cases}</testsuite>')
    counts, failing = _summarize_junit(str(report))
    assert len(failing) == 25

    out = _format_structured(1, counts, failing)
    lines = out.splitlines()
    listed = [ln for ln in lines if ln.startswith("m::t")]
    assert len(listed) == 20
    assert listed == [f"m::t{i}" for i in range(20)]
    assert "... and 5 more failing tests" in lines
    # No 21st name leaks past the cap.
    assert "m::t20" not in listed


def test_run_tests_pytest_command_without_report_falls_back_to_tail(tmp_path):
    """Finding 8: a command containing 'pytest' (so structured=True) whose run
    produces no valid junit report degrades to the exit-code + output tail,
    never leaking the temp report path into the transcript."""
    # 'pytest' appears only in a shell comment, which also swallows the
    # appended --junitxml=<path> arg, so no report is ever written.
    command = (
        f'{shlex.quote(sys.executable)} '
        '-c "import sys; print(\'no junit here\'); sys.exit(4)" #pytest'
    )
    rt, source = _runtime([_RUN_TESTS, _finish("ok")], tmp_path, test_command=command)
    rt.run(TaskSubmission(task="run the tests"), task_id="rt9")
    obs = source.requests[1].messages[-1]["content"]
    assert "exit_code=4" in obs
    assert "no structured results" in obs
    assert "no junit here" in obs
    # The temp junit path must not leak into the model transcript.
    assert "agentconnect-junit-" not in obs
    assert "--junitxml" not in obs


# -------------------------------------------- integration with the stub backend
def test_runs_against_model_manager_stub_backend(tmp_path):
    """The ModelSource seam is the ModelBackend interface: the loop must run
    against the stub backend unchanged. Stub output is prose, so the run
    resolves as a free-form finish."""
    from agentconnect.model_manager.backends import StubBackend

    backend = StubBackend()
    backend.load("qwen3.6-35b-a3b")
    rt = LangGraphAgentRuntime(backend, RuntimeConfig(workspace_root=str(tmp_path)))
    result = rt.run(TaskSubmission(task="summarize the repo"), task_id="t10")
    assert result.status in ("completed", "incomplete")
    assert result.summary  # stub always says something
