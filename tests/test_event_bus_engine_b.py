"""Engine B -> ecosystem event bus bridge (docs/EVENT_BUS.md §3).

Engine B (`RouterService`'s task pipeline over `SharedMemory.transition_task`
+ the federated `WorkQueue`'s ticket lifecycle) runs on its OWN SQLite
connection, so its transitions can never ride Path 1's same-commit emission.
`SharedMemory.bind_event_bus(sink)` bridges them as ADVISORY `state.changed`
events (payload marked `engine: "b"`) into the core ledger's `event_log`:

* applied `task_state` transitions bridge (after their own commit);
* applied `ticket_status` lifecycle writes bridge (claim, report, reap, …);
* refused/noop outcomes do NOT bridge (parity with Path 1);
* unbound (the default) is a strict no-op — the pre-bridge behaviour;
* a raising sink never breaks an Engine B write (advisory by construction);
* bridge payloads carry NO free text (`reason` is deliberately omitted —
  no privacy tier is in scope at that layer to gate it).
"""

from __future__ import annotations

import pytest

from agentconnect.common.config import load_routing
from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import TaskState
from agentconnect.common.state import TASK_STATE_VOCAB
from agentconnect.common.transitions import TransitionAuthority
from agentconnect.common.workqueue import WorkQueue
from agentconnect.core.storage import SqliteStorage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _bridged(tmp_path):
    """A fresh Engine B store + queue, bridged into a fresh core ledger."""
    storage = SqliteStorage(str(tmp_path / "core-ledger.db"))
    mem = SharedMemory()
    mem.bind_event_bus(storage.append_bus_event)
    wq = WorkQueue(mem, load_routing())
    return storage, mem, wq


def _bridge_events(storage, vocabulary=None):
    rows = storage.list_bus_events(types=["state.changed"], limit=500)
    rows = [r for r in rows if r["payload"].get("engine") == "b"]
    if vocabulary:
        rows = [r for r in rows if r["payload"].get("vocabulary") == vocabulary]
    return rows


def test_engine_b_task_pipeline_transitions_bridge_onto_the_bus(tmp_path):
    storage, mem, _ = _bridged(tmp_path)
    authority = TransitionAuthority(vocabulary=TASK_STATE_VOCAB,
                                    writer=mem.transition_task)
    task_id = mem.create_task({"task": "t"})

    authority.transition(task_id, TaskState.CLASSIFIED, actor="router")
    authority.transition(task_id, TaskState.PRIVACY_CHECKED, actor="router")

    rows = _bridge_events(storage, vocabulary="task_state")
    assert [(r["payload"]["src"], r["payload"]["dst"]) for r in rows] == [
        ("CREATED", "CLASSIFIED"), ("CLASSIFIED", "PRIVACY_CHECKED"),
    ]
    assert all(r["task_id"] == task_id and r["entity_id"] == task_id for r in rows)
    assert all(r["outcome"] == "applied" for r in rows)
    # No free text rides a bridge payload — enums/ids and the engine marker only.
    assert all(set(r["payload"].keys()) == {"vocabulary", "src", "dst", "engine"}
               for r in rows)


def test_engine_b_noop_and_refused_transitions_do_not_bridge(tmp_path):
    storage, mem, _ = _bridged(tmp_path)
    authority = TransitionAuthority(vocabulary=TASK_STATE_VOCAB,
                                    writer=mem.transition_task)
    task_id = mem.create_task({"task": "t"})
    authority.transition(task_id, TaskState.CLASSIFIED, actor="router")
    before = len(_bridge_events(storage))

    # noop: already at destination
    authority.transition(task_id, TaskState.CLASSIFIED, actor="router")
    # applied cancel, then a second cancel = no-op success (terminal is final)
    authority.cancel(task_id, cancelled=TaskState.CANCELLED, actor="router")
    authority.cancel(task_id, cancelled=TaskState.CANCELLED, actor="router")

    rows = _bridge_events(storage)
    # exactly one more applied bridge event (the cancel), nothing for noop/refused
    assert len(rows) == before + 1
    assert rows[-1]["payload"]["dst"] == "CANCELLED"


def test_ticket_lifecycle_bridges_claim_onto_the_bus(tmp_path):
    storage, mem, wq = _bridged(tmp_path)
    ticket = wq.add(task="t", origin="test", privacy_class="public", payload="p")

    claimed = wq.claim("trusted-worker", "local_only", ticket["ticket_id"])
    assert "error" not in claimed, claimed

    rows = _bridge_events(storage, vocabulary="ticket_status")
    assert rows, "an applied ticket claim must bridge onto the bus"
    assert rows[-1]["entity_id"] == ticket["ticket_id"]
    assert (rows[-1]["payload"]["src"], rows[-1]["payload"]["dst"]) == ("open", "claimed")
    # reason (free text at this layer) never rides the bridge payload
    assert "reason" not in rows[-1]["payload"]


def test_unbound_bridge_is_a_noop_and_a_raising_sink_never_breaks_engine_b(tmp_path):
    storage = SqliteStorage(str(tmp_path / "core-ledger.db"))

    # Unbound: no bus rows, no errors — the pre-bridge behaviour exactly.
    mem = SharedMemory()
    authority = TransitionAuthority(vocabulary=TASK_STATE_VOCAB,
                                    writer=mem.transition_task)
    task_id = mem.create_task({"task": "t"})
    authority.transition(task_id, TaskState.CLASSIFIED, actor="router")
    assert storage.list_bus_events(limit=10) == []

    # A raising sink: the Engine B write still lands (advisory by construction).
    def _boom_sink(**kwargs):
        raise RuntimeError("bus down")

    mem2 = SharedMemory()
    mem2.bind_event_bus(_boom_sink)
    authority2 = TransitionAuthority(vocabulary=TASK_STATE_VOCAB,
                                     writer=mem2.transition_task)
    t2 = mem2.create_task({"task": "t"})
    authority2.transition(t2, TaskState.CLASSIFIED, actor="router")
    assert mem2.get_task(t2)["state"] == "CLASSIFIED"


def test_create_app_binds_the_bridge_when_router_and_ledger_coexist(tmp_path):
    """The API deployment wires `router.memory.bind_event_bus(...)`
    automatically whenever it holds both planes."""
    from types import SimpleNamespace

    from agentconnect.api.app import create_app
    from agentconnect.core import AgentConnectService, EchoWorker

    svc = AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "art"),
        workers=[EchoWorker()],
    )
    mem = SharedMemory()
    fake_router = SimpleNamespace(memory=mem)
    create_app(service=svc, router=fake_router)
    assert mem._event_bus is not None

    authority = TransitionAuthority(vocabulary=TASK_STATE_VOCAB,
                                    writer=mem.transition_task)
    task_id = mem.create_task({"task": "t"})
    authority.transition(task_id, TaskState.CLASSIFIED, actor="router")
    rows = [r for r in svc.storage.list_bus_events(types=["state.changed"], limit=100)
            if r["payload"].get("engine") == "b"]
    assert rows and rows[-1]["task_id"] == task_id
