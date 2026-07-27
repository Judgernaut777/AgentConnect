"""Event-bus payload privacy (docs/EVENT_BUS.md §6, Part 1 slice).

The one free-text field a subtask-lifecycle event carries is `title` — an
operator-chosen string that can hold content, unlike the ids/enums the rest of
an event's metadata holds. `AgentConnectService._subtask_event_meta` withholds
it once a subtask's privacy strictness reaches the `local_only`/
`secret_sensitive` band, at WRITE time — so it is never persisted into the
durable `event_log` in the first place (fail-closed: nothing to redact later
if it was never stored).
"""

from __future__ import annotations

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    PrivacyTier,
    SubtaskRequest,
)


def _svc(tmp_path) -> AgentConnectService:
    return AgentConnectService.create(
        db_path=str(tmp_path / "l.db"), artifact_dir=str(tmp_path / "a"),
        workers=[EchoWorker()],
    )


def _subtask_created_payload(svc, task_id: str) -> dict:
    events = [e for e in svc.list_bus_events(task_id=task_id, limit=500)
              if e["type"] == "subtask.created"]
    assert events, "no subtask.created event was recorded"
    return events[-1]["payload"]


@pytest.mark.parametrize("tier", [PrivacyTier.secret_sensitive, PrivacyTier.local_only])
def test_title_withheld_for_strict_tiers(tmp_path, tier):
    # `subtask.created`'s `title` field, gated by `_subtask_event_meta`. The
    # worker's own result summary (which EchoWorker fills with this very title)
    # is gated by the sibling `_tier_gated_free_text` rule — pinned separately
    # below, and the final assertion here sweeps EVERY bus event for the task.
    canary = "CANARY_do_not_leak_this_title"
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(
        title=canary, instructions="x", privacy_tier=tier))

    payload = _subtask_created_payload(svc, task.id)
    assert payload["title"] == "[redacted]"
    assert canary not in str(payload)
    # ... and nowhere else on the bus either (any type, any payload key).
    assert canary not in str(svc.list_bus_events(task_id=task.id, limit=500))


@pytest.mark.parametrize("tier", [PrivacyTier.secret_sensitive, PrivacyTier.local_only])
def test_worker_summary_withheld_for_strict_tiers(tmp_path, tier):
    """`EchoWorker` echoes the subtask title verbatim into its result summary —
    the exact worker-supplied free-text path that must never carry a
    strict-tier subtask's content into the durable `event_log`
    (docs/EVENT_BUS.md §6: `_tier_gated_free_text`)."""
    canary = "CANARY_summary_leak_5c1"
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title=canary, instructions="x", privacy_tier=tier))
    assert sub.status.value == "succeeded"  # the worker really ran and reported

    events = svc.list_bus_events(task_id=task.id, limit=500)
    terminal = [e for e in events
                if e["type"] in ("worker.completed", "worker.failed",
                                 "subtask.completed", "subtask.failed")]
    assert terminal, "the run must have produced terminal rich events"
    for e in terminal:
        assert e["payload"].get("summary") == "[redacted]"
    assert canary not in str(events)


def test_worker_summary_present_for_looser_tiers(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(
        title="plain", instructions="x", privacy_tier=PrivacyTier.repo_sensitive))
    done = [e for e in svc.list_bus_events(task_id=task.id, limit=500)
            if e["type"] == "subtask.completed"]
    assert done and "plain" in done[-1]["payload"]["summary"]


@pytest.mark.parametrize("tier", [PrivacyTier.secret_sensitive, PrivacyTier.local_only])
def test_deny_reason_withheld_for_strict_tiers(tmp_path, tier):
    """An operator denying a strict-tier subtask often restates WHY in the
    free-text reason — that explanation must never reach the durable bus
    (docs/EVENT_BUS.md §6)."""
    from agentconnect.core import RawModelWorker, WorkerLocation

    # A local worker that demands human approval parks ANY tier (including
    # secret_sensitive, which never routes to cloud) in `needs_approval`.
    gated = RawModelWorker("gated", lambda p: "out", model="m",
                           location=WorkerLocation.local,
                           privacy_tiers=list(PrivacyTier),
                           requires_approval=True)
    svc2 = AgentConnectService.create(
        db_path=str(tmp_path / "deny.db"), artifact_dir=str(tmp_path / "deny-art"),
        workers=[gated],
    )
    canary = "CANARY_deny_reason_ab2 it mentions the secret thing"
    task = svc2.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc2.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=tier))
    assert sub.status.value == "needs_approval"

    svc2.deny_subtask(sub.id, "operator", reason=canary)

    denied = [e for e in svc2.list_bus_events(task_id=task.id, limit=500)
              if e["type"] == "subtask.denied"]
    assert denied
    assert denied[-1]["payload"]["reason"] == "[redacted]"
    assert canary not in str(svc2.list_bus_events(task_id=task.id, limit=500))


def test_deny_reason_present_for_looser_tiers(tmp_path):
    from agentconnect.core import RawModelWorker, WorkerLocation

    gated = RawModelWorker("gated", lambda p: "out", model="m",
                           location=WorkerLocation.local,
                           privacy_tiers=list(PrivacyTier),
                           requires_approval=True)
    svc = AgentConnectService.create(
        db_path=str(tmp_path / "deny2.db"), artifact_dir=str(tmp_path / "deny2-art"),
        workers=[gated],
    )
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    sub = svc.submit_subtask(task.id, SubtaskRequest(
        title="t", instructions="i", privacy_tier=PrivacyTier.public))
    assert sub.status.value == "needs_approval"
    svc.deny_subtask(sub.id, "operator", reason="too expensive today")
    denied = [e for e in svc.list_bus_events(task_id=task.id, limit=500)
              if e["type"] == "subtask.denied"]
    assert denied and denied[-1]["payload"]["reason"] == "too expensive today"


@pytest.mark.parametrize("tier", [PrivacyTier.public, PrivacyTier.public_redacted,
                                  PrivacyTier.repo_sensitive])
def test_title_present_for_looser_tiers(tmp_path, tier):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(
        title="plain-title", instructions="x", privacy_tier=tier))

    payload = _subtask_created_payload(svc, task.id)
    assert payload["title"] == "plain-title"


def test_state_changed_payload_never_carries_title_at_all(tmp_path):
    """The structural `state.changed` skeleton's payload is, by construction,
    only `{vocabulary, src, dst, reason}` — a subtask's title is never even a
    candidate for that path, independent of the Path 2 redaction above."""
    canary = "CANARY_state_changed_must_not_see_this"
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.submit_subtask(task.id, SubtaskRequest(
        title=canary, instructions="x", privacy_tier=PrivacyTier.secret_sensitive))
    changed = [e for e in svc.list_bus_events(task_id=task.id, limit=500)
              if e["type"] == "state.changed"]
    assert changed
    for e in changed:
        assert set(e["payload"].keys()) == {"vocabulary", "src", "dst", "reason"}
    assert canary not in str(changed)
