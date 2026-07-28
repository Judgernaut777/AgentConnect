"""Multi-product publish ingress: `POST /events`, source-scoped publish
tokens, and store-side privacy re-redaction (docs/EVENT_BUS.md, shared event
bus contract v1).

Layout mirrors `test_event_bus.py`/`test_event_bus_privacy.py`:

* storage-level: `source_product` column/backfill/filter, `event_id` dedup
  under the ingest path too.
* service-level: `mint_publish_token`, `authorize("publish_event", ...)`
  binding (the anti-forgery property).
* HTTP-level: `POST /events` round-trip, cross-product forgery 403, privacy
  redaction, `GET /events`/`GET /events/stream` `source_product` filters,
  existing (no-filter) consumers still see everything.
"""

from __future__ import annotations

import json

import pytest

from agentconnect.core import (
    AgentConnectService,
    CreateTaskRequest,
    EchoWorker,
    PolicyViolation,
    PrivacyTier,
)
from agentconnect.core.errors import InvalidRequest
from agentconnect.api.routes_events import sse_lines
from conftest import operator_client  # noqa: E402


def _svc(tmp_path, **kwargs) -> AgentConnectService:
    return AgentConnectService.create(
        db_path=str(tmp_path / "ledger.db"), artifact_dir=str(tmp_path / "art"),
        workers=kwargs.pop("workers", [EchoWorker()]), **kwargs,
    )


# ------------------------------------------------------------- storage layer
def test_existing_rows_backfill_source_product_agentconnect(tmp_path):
    """Every event AgentConnect ever wrote for itself really did originate
    inside AgentConnect — including rows written before `source_product`
    existed as a concept (the migration/backfill case)."""
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    events = svc.list_bus_events(limit=500)
    assert events
    assert all(e["source_product"] == "agentconnect" for e in events)


def test_migration_backfills_a_pre_existing_database(tmp_path):
    """A database created before `source_product` existed (simulated: open,
    write, close, then reopen — the real migration path runs at every
    `SqliteStorage.__init__`, so reopening a file DB always re-runs it, and
    it must be idempotent)."""
    from agentconnect.core.storage import SqliteStorage

    db_path = str(tmp_path / "pre.db")
    storage = SqliteStorage(db_path)
    storage.append_bus_event(event_id="ev-pre-1", type="task.created", actor="x")
    storage.close()

    reopened = SqliteStorage(db_path)
    rows = reopened.list_bus_events(limit=500)
    assert rows and rows[0]["source_product"] == "agentconnect"
    # Re-running the migration on an already-migrated DB must not explode.
    reopened.close()
    SqliteStorage(db_path).close()


def test_append_bus_event_ingested_dedups_by_event_id(tmp_path):
    from agentconnect.core.storage import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "l.db"))
    seq1 = storage.append_bus_event_ingested(
        event_id="ev-x", type="capability.grant.issued", source_product="toolconnect",
        payload={"grant_id": "g1"}, privacy_tier="public",
    )
    dup = storage.append_bus_event_ingested(
        event_id="ev-x", type="capability.grant.issued", source_product="toolconnect",
        payload={"grant_id": "g1"}, privacy_tier="public",
    )
    assert seq1 is not None
    assert dup is None


def test_list_bus_events_source_product_filter(tmp_path):
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.storage.append_bus_event_ingested(
        event_id="ev-tc-1", type="grant.issued", source_product="toolconnect",
        payload={"grant_id": "g1"}, privacy_tier="public",
    )
    svc.storage.append_bus_event_ingested(
        event_id="ev-cc-1", type="compute.generation.placed", source_product="computeconnect",
        payload={"provider": "p1"}, privacy_tier="public",
    )

    only_tool = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert only_tool and all(e["source_product"] == "toolconnect" for e in only_tool)

    both = svc.list_bus_events(source_products=["toolconnect", "computeconnect"], limit=500)
    assert {e["source_product"] for e in both} == {"toolconnect", "computeconnect"}

    # Existing (no-filter) consumers still see everything, internal + foreign.
    everyone = svc.list_bus_events(limit=500)
    assert {"agentconnect", "toolconnect", "computeconnect"} <= {
        e["source_product"] for e in everyone
    }


# ---------------------------------------------------- privacy re-redaction
@pytest.mark.parametrize("tier", [PrivacyTier.secret_sensitive, PrivacyTier.local_only])
def test_ingested_strict_tier_payload_is_dropped_to_a_marker(tmp_path, tier):
    canary = "CANARY_ingested_secret_payload_must_not_leak"
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-secret", type="grant.issued", source_product="toolconnect",
        payload={"detail": canary}, privacy_tier=tier.value,
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert events
    payload = events[-1]["payload"]
    assert canary not in json.dumps(payload)
    assert canary not in json.dumps(events)
    assert set(payload.keys()) == {"redacted"}


def test_ingested_unknown_tier_fails_closed_to_redacted(tmp_path):
    canary = "CANARY_unknown_tier_must_redact"
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-unknown-tier", type="grant.issued", source_product="toolconnect",
        payload={"detail": canary}, privacy_tier="not_a_real_tier",
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert canary not in json.dumps(events)
    assert set(events[-1]["payload"].keys()) == {"redacted"}


def test_ingested_missing_tier_fails_closed_to_redacted(tmp_path):
    canary = "CANARY_missing_tier_must_redact"
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-no-tier", type="grant.issued", source_product="toolconnect",
        payload={"detail": canary}, privacy_tier=None,
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert canary not in json.dumps(events)
    assert set(events[-1]["payload"].keys()) == {"redacted"}


def test_ingested_looser_tier_payload_passes_through_scrubbed(tmp_path):
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-public", type="grant.issued", source_product="toolconnect",
        payload={"grant_id": "g1", "note": "plain"}, privacy_tier="public",
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    payload = events[-1]["payload"]
    assert payload.get("grant_id") == "g1"
    assert payload.get("note") == "plain"


def test_ingested_credential_shaped_key_still_masked_even_at_public_tier(tmp_path):
    """Defense in depth (§6): the metadata scrubber still runs on a looser-tier
    ingested payload, exactly as it does for internal events."""
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-cred", type="grant.issued", source_product="toolconnect",
        payload={"api_key": "sk-super-secret", "grant_id": "g1"}, privacy_tier="public",
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert events[-1]["payload"]["api_key"] == "[redacted]"
    assert "sk-super-secret" not in json.dumps(events)


def test_ingested_nested_credentials_masked_at_any_depth_at_public_tier(tmp_path):
    """Regression: the store-side scrubber must recurse. A hostile/buggy
    publisher can bury credentials inside nested objects/arrays and declare a
    loose tier; the fail-closed re-redaction masks credential-shaped keys at
    every depth so raw secrets never reach the readable stream (docs/EVENT_BUS.md
    §9.3, PRIVACY clause)."""
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-nested-cred", type="grant.issued", source_product="toolconnect",
        payload={
            "grant_id": "g1",
            "details": {"api_key": "sk-live-NESTED_SECRET", "password": "hunter2"},
            "nested_list": [{"token": "abcd-NESTED_SECRET"}],
        },
        privacy_tier="public",
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    payload = events[-1]["payload"]
    assert payload["grant_id"] == "g1"
    assert payload["details"]["api_key"] == "[redacted]"
    assert payload["details"]["password"] == "[redacted]"
    assert payload["nested_list"][0]["token"] == "[redacted]"
    assert "NESTED_SECRET" not in json.dumps(events)
    assert "hunter2" not in json.dumps(events)


def test_ingested_deeply_nested_payload_fails_closed_not_recurses_forever(tmp_path):
    """A pathologically deep payload must not exhaust the stack, and anything
    below the depth bound is withheld rather than passed through unscrubbed."""
    canary = "CANARY_deep_secret_must_not_leak"
    deep: dict = {"leaf": canary}
    for _ in range(200):
        deep = {"level": deep}
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-deep", type="grant.issued", source_product="toolconnect",
        payload=deep, privacy_tier="public",
    )
    events = svc.list_bus_events(source_products=["toolconnect"], limit=500)
    assert canary not in json.dumps(events)


# --------------------------------------------------------- mint + authorize
def test_mint_publish_token_rejects_unknown_source_product(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(InvalidRequest):
        svc.mint_publish_token("not_a_real_product")


def test_publish_token_can_only_publish_as_its_own_source_product(tmp_path):
    svc = _svc(tmp_path)
    token = svc.mint_publish_token("toolconnect")
    scope = svc.authorize(
        token.plaintext, "publish_event", source_product="toolconnect",
    )
    assert scope["source_product"] == "toolconnect"

    with pytest.raises(PolicyViolation):
        svc.authorize(token.plaintext, "publish_event", source_product="computeconnect")


def test_publish_token_cannot_reach_any_other_action(tmp_path):
    svc = _svc(tmp_path)
    token = svc.mint_publish_token("toolconnect")
    with pytest.raises(PolicyViolation):
        svc.authorize(token.plaintext, "list_tasks")


def test_operator_and_manager_tokens_cannot_publish(tmp_path):
    svc = _svc(tmp_path)
    task = svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    op = svc.mint_operator_token("operator")
    mgr_token = svc.launch_session("claude", task_id=task.id, claim=True)["token"]

    with pytest.raises(PolicyViolation):
        svc.authorize(op.plaintext, "publish_event", source_product="toolconnect")
    with pytest.raises(PolicyViolation):
        svc.authorize(mgr_token, "publish_event", source_product="toolconnect")


# --------------------------------------------------------------------- HTTP
def _publish_client(svc, source_product: str):
    """A `TestClient` carrying a publish token scoped to `source_product`."""
    from fastapi.testclient import TestClient

    from agentconnect.api.app import create_app

    token = svc.mint_publish_token(source_product)
    client = TestClient(create_app(service=svc))
    client.headers.update({"Authorization": f"Bearer {token.plaintext}"})
    return client


def test_post_events_round_trips_through_get(tmp_path):
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")

    resp = client.post("/events", json={
        "type": "grant.issued",
        "source_product": "toolconnect",
        "payload": {"grant_id": "g1"},
        "privacy_tier": "public",
        "actor": "toolconnect-governor",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "seq" in body and "event_id" in body
    assert body["seq"] is not None

    reader = operator_client(svc)
    got = reader.get("/events", params={"source_product": "toolconnect"})
    assert got.status_code == 200, got.text
    events = got.json()["events"]
    assert any(e["event_id"] == body["event_id"] for e in events)
    published = next(e for e in events if e["event_id"] == body["event_id"])
    assert published["source_product"] == "toolconnect"
    assert published["type"] == "grant.issued"
    assert published["payload"]["grant_id"] == "g1"
    assert published["actor"] == "toolconnect-governor"


def test_post_events_idempotent_replay_via_client_supplied_event_id(tmp_path):
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")
    body = {
        "type": "grant.issued", "source_product": "toolconnect",
        "event_id": "publisher-chosen-id-1", "payload": {}, "privacy_tier": "public",
    }
    first = client.post("/events", json=body)
    assert first.status_code == 201
    assert first.json()["seq"] is not None

    second = client.post("/events", json=body)
    assert second.status_code == 201
    assert second.json()["event_id"] == "publisher-chosen-id-1"
    assert second.json()["seq"] is None  # no new row: idempotent duplicate


def test_post_events_cross_product_forgery_is_403(tmp_path):
    """A token scoped to publish as `toolconnect` can never write a row
    claiming `computeconnect` — the anti-forgery property the contract exists
    for."""
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")
    resp = client.post("/events", json={
        "type": "compute.generation.placed",
        "source_product": "computeconnect",
        "payload": {}, "privacy_tier": "public",
    })
    assert resp.status_code == 403, resp.text
    # Nothing was written under the forged identity.
    reader = operator_client(svc)
    got = reader.get("/events", params={"source_product": "computeconnect"})
    assert got.json()["events"] == []


def test_post_events_operator_token_cannot_publish(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    resp = client.post("/events", json={
        "type": "grant.issued", "source_product": "toolconnect",
        "payload": {}, "privacy_tier": "public",
    })
    assert resp.status_code == 403, resp.text


def test_post_events_anonymous_is_401(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    anon = client.__class__(client.app)
    resp = anon.post("/events", json={
        "type": "grant.issued", "source_product": "toolconnect",
        "payload": {}, "privacy_tier": "public",
    })
    assert resp.status_code == 401


def test_post_events_unknown_type_is_400(tmp_path):
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")
    resp = client.post("/events", json={
        "type": "not.a.real.type", "source_product": "toolconnect",
        "payload": {}, "privacy_tier": "public",
    })
    assert resp.status_code == 400


def test_post_events_unknown_source_product_is_400(tmp_path):
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")
    resp = client.post("/events", json={
        "type": "grant.issued", "source_product": "not_a_real_product",
        "payload": {}, "privacy_tier": "public",
    })
    assert resp.status_code == 400


def test_post_events_secret_sensitive_payload_dropped_at_the_http_boundary(tmp_path):
    canary = "CANARY_http_boundary_secret_payload"
    svc = _svc(tmp_path)
    client = _publish_client(svc, "toolconnect")
    resp = client.post("/events", json={
        "type": "grant.issued", "source_product": "toolconnect",
        "payload": {"detail": canary}, "privacy_tier": "secret_sensitive",
    })
    assert resp.status_code == 201

    reader = operator_client(svc)
    got = reader.get("/events", params={"source_product": "toolconnect"})
    assert canary not in got.text


def test_get_events_unknown_source_product_filter_is_400(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    resp = client.get("/events", params={"source_product": "not_a_real_product"})
    assert resp.status_code == 400


def test_get_events_no_source_product_filter_still_sees_everything(tmp_path):
    """Existing consumers (no `source_product` filter at all) are unaffected —
    they still see every internal AND foreign event interleaved."""
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    publisher = _publish_client(svc, "toolconnect")
    publisher.post("/events", json={
        "type": "grant.issued", "source_product": "toolconnect",
        "payload": {"grant_id": "g1"}, "privacy_tier": "public",
    })

    reader = operator_client(svc)
    resp = reader.get("/events", params={"limit": 500})
    events = resp.json()["events"]
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # seq stays monotonic
    products = {e["source_product"] for e in events}
    assert "agentconnect" in products
    assert "toolconnect" in products


def test_sse_stream_source_product_filter_only_yields_matching_events(tmp_path):
    svc = _svc(tmp_path)
    svc.create_task(CreateTaskRequest(title="T", created_by="human"))
    svc.storage.append_bus_event_ingested(
        event_id="ev-sse-tc", type="grant.issued", source_product="toolconnect",
        payload={}, privacy_tier="public",
    )

    collected = []
    ticks = [0]

    def stop():
        ticks[0] += 1
        return len(collected) >= 1 or ticks[0] > 50

    for chunk in sse_lines(svc, 0, None, 0.001, should_stop=stop,
                           source_products=["toolconnect"]):
        if chunk.startswith("id:"):
            collected.append(chunk)
    assert collected
    data = [json.loads(c.split("data: ", 1)[1]) for c in collected
            if "data: " in c]
    assert all(d["source_product"] == "toolconnect" for d in data)


def test_sse_stream_source_product_query_param_through_the_real_app(tmp_path):
    svc = _svc(tmp_path)
    svc.storage.append_bus_event_ingested(
        event_id="ev-app-tc", type="grant.issued", source_product="toolconnect",
        payload={}, privacy_tier="public",
    )
    svc.storage.append_bus_event_ingested(
        event_id="ev-app-cc", type="compute.generation.placed",
        source_product="computeconnect", payload={}, privacy_tier="public",
    )
    client = operator_client(svc)
    client.app.state.sse_poll_interval = 0.01
    seen = {"n": 0}

    def stop():
        seen["n"] += 1
        return seen["n"] > 3

    client.app.state.sse_should_stop = stop
    with client.stream("GET", "/events/stream",
                       params={"since": 0, "source_product": "toolconnect"}) as resp:
        assert resp.status_code == 200
        lines = list(resp.iter_lines())
    data = [json.loads(l[len("data: "):]) for l in lines if l.startswith("data: ")]
    assert data and all(d["source_product"] == "toolconnect" for d in data)


def test_sse_stream_unknown_source_product_filter_is_400_through_the_app(tmp_path):
    svc = _svc(tmp_path)
    client = operator_client(svc)
    resp = client.get("/events/stream", params={"source_product": "bogus"})
    assert resp.status_code == 400
