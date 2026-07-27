"""One shared vocabulary mapping ticket state <-> task state <-> subtask
status (goal item 3), documented in code and tested here.

(a) `TICKET_TO_TASK_STATE` — the only ticket-status -> task-state translation
    any code performs, walked end-to-end through a real `WorkQueue` +
    `SharedMemory` pair.
(b) `SUBTASK_RUN_PAIRING` — subtask <-> run pairing, exercised end-to-end
    through a real `AgentConnectService` echo-worker run.
(c) `TASK_ROSETTA` — the documentary Engine-A <-> Engine-B rosetta stone,
    tested only for totality (never enforced at a write site).

Plus a generic sanity sweep over every registered `Vocabulary`: every enum
member is a key in `edges`, terminal members have empty edge sets, and
`universal` targets are terminal-or-declared.
"""

from __future__ import annotations

import time

import pytest

from agentconnect.common.config import load_routing
from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import TaskState
from agentconnect.common.workqueue import (
    TICKET_TO_TASK_STATE,
    TicketStatus,
    WorkQueue,
)
from agentconnect.core import AgentConnectService, CreateTaskRequest, EchoWorker
from agentconnect.core.models import RunStatus, SubtaskRequest, SubtaskStatus, TaskStatus
from agentconnect.core.transition_vocab import (
    APPROVAL_STATUS_VOCAB,
    EXECUTION_STATE_VOCAB,
    REVIEW_STATUS_VOCAB,
    RUN_STATUS_VOCAB,
    SESSION_STATUS_VOCAB,
    SUBTASK_RUN_PAIRING,
    SUBTASK_STATUS_VOCAB,
    TASK_ROSETTA,
    TASK_STATUS_VOCAB,
)


def _queue():
    mem = SharedMemory()
    return mem, WorkQueue(mem, load_routing())


# --------------------------------------------------------------------- (a)
def test_ticket_report_trusted_maps_to_complete():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.done].value


def test_ticket_report_untrusted_maps_to_review_ready():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    got = wq.claim("friend", "external", ticket["ticket_id"])
    wq.report("friend", "external", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.in_review].value


def test_ticket_report_fail_exhausted_maps_to_failed():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id, max_attempts=1)
    got = wq.claim_next("worker-1", "local_only", capabilities=[])[0]
    wq.report("worker-1", "local_only", ticket["ticket_id"], got["lease_token"],
              {"status": "failed"})
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.failed].value


def test_ticket_reject_terminal_maps_to_failed():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p",
                    task_id=task_id, max_attempts=1)
    got = wq.claim("friend", "external", ticket["ticket_id"])
    wq.report("friend", "external", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    wq.reject("reviewer", "local_only", ticket["ticket_id"], reason="bad")
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.failed].value


def test_ticket_approve_maps_to_complete():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    ticket = wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    got = wq.claim("friend", "external", ticket["ticket_id"])
    wq.report("friend", "external", ticket["ticket_id"], got["lease_token"],
              {"status": "completed"})
    wq.approve("reviewer", "local_only", ticket["ticket_id"])
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.done].value


def test_ticket_reap_park_exhausted_maps_to_failed():
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id,
           max_attempts=1)
    wq.claim_next("worker-1", "local_only", capabilities=[], lease_seconds=1)
    wq.reap_expired(now=time.time() + 3600)
    assert mem.get_task(task_id)["state"] == TICKET_TO_TASK_STATE[TicketStatus.failed].value


def test_ticket_cancel_never_drives_task_state():
    """`cancelled` is intentionally ABSENT from `TICKET_TO_TASK_STATE`: a
    task's own cancel drives the ticket's cancel, never the reverse."""
    assert TicketStatus.cancelled not in TICKET_TO_TASK_STATE
    mem, wq = _queue()
    task_id = mem.create_task({"task": "x"})
    # Seed via the authority's LockedWriter — a bare update_task(state=...)
    # is rejected at runtime (one transition authority enforcement).
    mem.transition_task(task_id, lambda _cur: ({"state": "RUNNING"}, None))
    wq.add(task="x", origin="t", privacy_class="public", payload="p", task_id=task_id)
    wq.cancel_for_task(task_id)
    # The task-state write only ever happens via RouterService.cancel_task's
    # OWN authority call, not as a side effect of the ticket-side cancel.
    assert mem.get_task(task_id)["state"] == "RUNNING"


# --------------------------------------------------------------------- (b)
def test_subtask_run_pairing_end_to_end(tmp_path):
    svc = AgentConnectService.create(
        db_path=":memory:", artifact_dir=str(tmp_path / "a"), workers=[EchoWorker()],
    )
    task = svc.create_task(CreateTaskRequest(title="t", goal="g"))
    subtask = svc.submit_subtask(task.id, SubtaskRequest(title="s", instructions="i"))
    assert subtask.status is SubtaskStatus.succeeded
    runs = svc.storage.list_runs(subtask.id)
    assert len(runs) == 1
    assert SUBTASK_RUN_PAIRING[subtask.status] == runs[0].status


def test_subtask_run_pairing_totality():
    for subtask_status, run_status in SUBTASK_RUN_PAIRING.items():
        assert isinstance(subtask_status, SubtaskStatus)
        assert isinstance(run_status, RunStatus)
    # Every RunStatus member is paired to by exactly one SubtaskStatus.
    assert set(SUBTASK_RUN_PAIRING.values()) == set(RunStatus)


# --------------------------------------------------------------------- (c)
def test_task_rosetta_totality():
    assert set(TASK_ROSETTA.keys()) == set(TaskState)
    for v in TASK_ROSETTA.values():
        assert isinstance(v, TaskStatus)


def test_task_rosetta_terminal_agreement():
    """Every TaskState terminal maps to the matching TaskStatus terminal."""
    assert TASK_ROSETTA[TaskState.COMPLETE] is TaskStatus.succeeded
    assert TASK_ROSETTA[TaskState.CANCELLED] is TaskStatus.cancelled
    assert TASK_ROSETTA[TaskState.FAILED] is TaskStatus.failed


# --------------------------------------------------------- vocabulary sanity
_ALL_VOCABS = [
    TASK_STATUS_VOCAB, SUBTASK_STATUS_VOCAB, RUN_STATUS_VOCAB, REVIEW_STATUS_VOCAB,
    APPROVAL_STATUS_VOCAB, SESSION_STATUS_VOCAB, EXECUTION_STATE_VOCAB,
]


@pytest.mark.parametrize("vocab", _ALL_VOCABS, ids=lambda v: v.name)
def test_vocabulary_every_member_has_edges_entry(vocab):
    for member in vocab.enum_type:
        assert member in vocab.edges, f"{vocab.name}: {member} missing from edges"


@pytest.mark.parametrize("vocab", _ALL_VOCABS, ids=lambda v: v.name)
def test_vocabulary_terminal_members_have_empty_edges(vocab):
    for member in vocab.terminal:
        assert vocab.edges.get(member, frozenset()) == frozenset(), (
            f"{vocab.name}: terminal member {member} has outgoing edges"
        )


@pytest.mark.parametrize("vocab", _ALL_VOCABS, ids=lambda v: v.name)
def test_vocabulary_universal_targets_are_terminal_or_declared(vocab):
    for target in vocab.universal:
        assert target in vocab.terminal or any(
            target in edges for edges in vocab.edges.values()
        ), f"{vocab.name}: universal target {target} is neither terminal nor reachable"
