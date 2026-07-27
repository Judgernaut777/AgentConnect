"""Enforcement (goal item 2/8): a state/status key must go through the shared
`TransitionAuthority`, never a bare `update_*()` call — runtime-provable (the
bare mutators on BOTH backends reject state keys) AND statically provable (the
source-scan tests below execute the spec's §8 grep checklist against
`packages/*/src` on every test run, so a consumer quietly reverting to a bare
state write — a raw `UPDATE ... SET status=` outside the two writer files, a
resurrected second-writer helper — fails the suite instead of waiting for a
human to re-run the checklist by hand).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentconnect.common.memory import SharedMemory
from agentconnect.core.storage import SqliteStorage


@pytest.fixture()
def storage():
    return SqliteStorage(":memory:")


@pytest.mark.parametrize(
    "method,table,key",
    [
        ("update_task", "tasks", "status"),
        ("update_subtask", "subtasks", "status"),
        ("update_run", "worker_runs", "status"),
        ("update_review", "reviews", "status"),
        ("update_approval", "approvals", "status"),
        ("update_session", "manager_sessions", "status"),
        ("update_execution", "executions", "state"),
    ],
)
def test_bare_mutator_rejects_state_key(storage, method, table, key):
    fn = getattr(storage, method)
    with pytest.raises(ValueError, match="TransitionAuthority"):
        fn("some_id", **{key: "whatever"})


@pytest.mark.parametrize(
    "method,other_field",
    [
        ("update_task", "handoff_summary"),
        ("update_subtask", "assigned_worker"),
        ("update_run", "error"),
        ("update_review", "result_artifact_id"),
        ("update_approval", "reason"),
        ("update_session", "shell_command"),
        ("update_execution", "detail"),
    ],
)
def test_bare_mutator_still_accepts_non_state_fields(storage, method, other_field):
    """The guard is scoped to the state/status column only — every other
    field write through the bare mutator is unaffected (a no-op UPDATE
    against a nonexistent row, not an error, since these are unit tests of
    the guard alone, not of referential integrity)."""
    fn = getattr(storage, method)
    fn("some_id", **{other_field: "x"})  # must not raise


def test_transition_row_bypasses_the_guard_by_construction(storage):
    """The ONE legitimate writer of the state/status column: `transition_row`
    (the authority's `LockedWriter`) is exempt by construction — it never
    calls the guarded `update_*` methods, so there is no path by which it
    could trip its own enforcement."""
    from agentconnect.core.models import Task

    task = Task(id="task_enforcement_1", title="t", status="queued")
    storage.insert_task(task)

    def decide(current_raw):
        assert current_raw == "queued"
        return {"status": "in_progress"}, None

    applied, stored = storage.transition_row("tasks", task.id, decide)
    assert applied is True
    assert stored == "in_progress"
    assert storage.get_task(task.id).status.value == "in_progress"


# --------------------------------------------------------------------- #
# Engine B: SharedMemory's bare mutator is guarded the same way
# --------------------------------------------------------------------- #
def test_shared_memory_update_task_rejects_state_key():
    """The Engine-B twin of `_reject_state_write`: `tasks.state` has exactly
    one writer (`transition_task`). Without this guard a bare
    `update_task(state=...)` silently skipped the fresh-read CAS, the FSM
    edge check, AND the same-commit audit row (confirmed live in review:
    CREATED jumped straight to terminal COMPLETE with zero `logs` rows)."""
    mem = SharedMemory()
    task_id = mem.create_task({"task": "x"})
    with pytest.raises(ValueError, match="TransitionAuthority"):
        mem.update_task(task_id, state="COMPLETE")
    # The illegal write did not land, and nothing was audited for it.
    assert mem.get_task(task_id)["state"] == "CREATED"
    assert mem.get_log_slice(task_id, max_lines=10) == []
    # Non-state fields still flow through the bare mutator unaffected.
    mem.update_task(task_id, summary="still fine")
    assert mem.get_task(task_id)["summary"] == "still fine"


def test_shared_memory_transition_task_is_exempt_by_construction():
    """The ONE legitimate `tasks.state` writer keeps working — and audits."""
    mem = SharedMemory()
    task_id = mem.create_task({"task": "x"})

    def decide(current_raw):
        assert current_raw == "CREATED"
        return {"state": "CLASSIFIED"}, None

    applied, stored = mem.transition_task(task_id, decide)
    assert applied is True
    assert stored == "CLASSIFIED"
    assert mem.get_task(task_id)["state"] == "CLASSIFIED"


def test_orphaned_boolean_cas_helpers_are_gone():
    """`update_subtask_if_status` / `update_run_if_status` were public methods
    that flipped a status column with no FSM check and no audit row — a second
    door past the authority. Every call site migrated onto the
    `subtask_status`/`run_status` authorities, so the doors were removed."""
    assert not hasattr(SqliteStorage, "update_subtask_if_status")
    assert not hasattr(SqliteStorage, "update_run_if_status")


# --------------------------------------------------------------------- #
# Static enforcement: the §8 grep checklist, executed by the suite
# --------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Files allowed to touch state columns directly: the two LockedWriter
#: backends and the WorkQueue's bespoke fenced ticket UPDATEs.
_WRITER_FILES = {
    ("agentconnect", "core", "storage.py"),
    ("agentconnect", "common", "memory.py"),
    ("agentconnect", "common", "workqueue.py"),
}


def _src_files():
    files = []
    for src_dir in sorted((_REPO_ROOT / "packages").glob("*/src")):
        for path in sorted(src_dir.rglob("*.py")):
            if "build" in path.parts:  # generated mirrors, never edited
                continue
            files.append(path)
    assert files, "source scan found no files — repo layout changed?"
    return files


def _is_writer_file(path: Path) -> bool:
    return tuple(path.parts[-3:]) in _WRITER_FILES


def test_scan_no_bare_state_kwarg_outside_writers():
    """Checklist item 1: no bare-mutator call passes a status/state keyword
    anywhere under `packages/*/src` outside the two writer backends.

    Two deliberate refinements over the checklist's raw grep, both verified
    against the tree when this test was written:
    - `update_execution` is only flagged with a `storage.` receiver:
      `service.update_execution(state=...)` is the AUTHORITY FUNNEL (it routes
      the state through `_execution_state_authority.converge`), and
      `DirectExecutionBackend`/the Temporal client legitimately call it.
    - every other `update_<entity>` is flagged on ANY receiver (there is no
      service-level funnel bearing those names; the runtime guards raise for
      the storage/memory ones regardless).
    """
    patterns = [
        re.compile(
            r"\.update_(task|subtask|run|review|approval|session)"
            r"\(\s*[^)]*\b(status|state)\s*="
        ),
        re.compile(r"storage\.update_execution\(\s*[^)]*\bstate\s*="),
    ]
    offenders = [
        f"{path}: {m.group(0)!r}"
        for path in _src_files()
        if not _is_writer_file(path)
        for pattern in patterns
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], "bare state/status writes outside the authority:\n" + "\n".join(offenders)


def test_scan_second_writer_symbol_stays_retired():
    """Checklist item 2: the WorkQueue's old second-writer helper name is
    gone from the tree (its replacement, `_mirror_task_state`, only shapes a
    `converge()` call). A genuinely separate bare-write helper cannot come
    back under the old name without failing this test."""
    symbol = "_set_task" + "_state"  # split so this file never matches itself
    offenders = [str(p) for p in _src_files() if symbol in p.read_text(encoding="utf-8")]
    assert offenders == [], f"retired symbol {symbol!r} resurfaced in: {offenders}"


def test_scan_orphaned_cas_helper_names_stay_gone():
    """Companion to `test_orphaned_boolean_cas_helpers_are_gone`, statically:
    neither a definition nor a call of the removed `*_if_status` helpers may
    reappear anywhere under `packages/*/src`."""
    pattern = re.compile(r"(def |\.)update_(subtask|run)_if_status\(")
    offenders = [
        f"{path}: {m.group(0)!r}"
        for path in _src_files()
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], "removed CAS helpers resurfaced:\n" + "\n".join(offenders)


def test_scan_raw_state_updates_confined_to_writer_files():
    """Checklist item 4: raw SQL that writes a status/state COLUMN of a
    state-carrying table exists only in the two LockedWriter backends and the
    WorkQueue's fenced set. (Refined from the checklist's raw grep: hand-rolled
    UPDATEs of NON-state columns — `claim_task`'s composed
    `SET current_manager=?, updated_at=?` in service.py — are legal; the
    invariant is about the state column, so the pattern requires it in the
    SET clause.)"""
    pattern = re.compile(
        r"UPDATE (tasks|subtasks|worker_runs|reviews|approvals|manager_sessions"
        r"|executions|work_queue) SET[^\"']*\b(status|state)\s*="
    )
    offenders = [
        f"{path}: {m.group(0)!r}"
        for path in _src_files()
        if not _is_writer_file(path)
        for m in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], "raw state-table UPDATE outside writer files:\n" + "\n".join(offenders)


def test_scan_temporal_adapter_never_touches_storage_directly():
    """Checklist item 3: the Temporal activities are an adapter — they never
    MUTATE through `service.storage.*` (the approval-expiry activity used to
    write `storage.update_approval` directly, violating the adapter boundary
    its own module docstring declares). Refined from the checklist's raw grep:
    a read-only config access (`worker_main`'s `service.storage.path`) is not
    a boundary violation; the pattern pins mutations."""
    pattern = re.compile(
        r"storage\.update_approval|service\.storage\.(update|insert|delete)_"
    )
    offenders = [
        str(path)
        for path in _src_files()
        if "agentconnect-temporal" in path.parts
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"temporal adapter touches storage directly: {offenders}"
