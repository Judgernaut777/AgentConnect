"""Linear mirror routes (spec §11, §14).

`/linear/webhook` needs no AgentConnect session token — it authenticates the
caller a different way: Linear signs every delivery (HMAC-SHA256 over the raw
body, hex digest in `Linear-Signature`), and `webhook()` below verifies that
signature against `LINEAR_WEBHOOK_SECRET` before doing anything else. That
signature *is* the credential for this route; see `authz.WEBHOOK_ROUTES`. When
the secret is not configured the route refuses to serve at all — it does not
fall back to accepting unsigned payloads. `/linear/sync` needs Linear API
credentials instead, and returns 503 when the deployment has not configured a
team.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from hashlib import sha256
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .routes_tasks import service

router = APIRouter(prefix="/linear", tags=["linear"])

_log = logging.getLogger(__name__)

#: Reject a webhook delivery whose own `webhookTimestamp` is further from "now"
#: than this many seconds. Best-effort replay protection: Linear includes this
#: field in most (not all) payload shapes, so its absence is not itself an error.
_MAX_DELIVERY_AGE_SECONDS = 60.0


class SyncBody(BaseModel):
    task_id: str


def _sync(request: Request):
    sync = getattr(request.app.state, "linear_sync", None)
    if sync is None:
        raise HTTPException(
            status_code=503,
            detail="Linear sync is not configured (set LINEAR_API_KEY and LINEAR_TEAM_ID)",
        )
    return sync


@router.post("/sync")
def sync_task(body: SyncBody, request: Request) -> dict[str, Any]:
    ref = _sync(request).sync_task(body.task_id)
    return ref.model_dump(mode="json")


@router.get("/tasks/{task_id}")
def get_linear_ref(task_id: str, request: Request) -> dict[str, Any]:
    ref = service(request).get_external_ref("task", task_id, "linear")
    if ref is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} is not synced to Linear")
    return ref.model_dump(mode="json")


def _webhook_secret(request: Request) -> str:
    """The configured `LINEAR_WEBHOOK_SECRET`, or a fail-closed 503.

    Unset is not "webhooks are off but harmless": with no secret there is nothing
    to verify a signature against, and silently accepting the payload anyway is
    exactly the bypass this route used to have. Refuse to serve instead, the same
    posture `deps.linear_sync_from_env` uses for an unconfigured Linear API key.
    """
    secret = getattr(request.app.state, "linear_webhook_secret", None)
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Linear webhook signature verification is not configured "
                "(set LINEAR_WEBHOOK_SECRET); refusing to accept unsigned webhooks"
            ),
        )
    return secret


def _verify_signature(secret: str, raw_body: bytes, signature: Optional[str]) -> None:
    """Constant-time compare of `hmac_sha256(secret, raw_body)` against the header.

    Verified against the *raw* bytes, before any JSON parsing — parsing first and
    re-serializing to check would let whitespace/key-order differences (which do
    not change the parsed payload) desync the digest from what Linear actually
    signed, or worse, from what a middlebox actually forwarded.
    """
    if not signature or not signature.strip():
        raise HTTPException(
            status_code=401, detail="missing Linear-Signature header"
        )
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise HTTPException(status_code=401, detail="invalid Linear webhook signature")


def _reject_stale_delivery(payload: dict[str, Any]) -> None:
    """Best-effort replay protection using Linear's own `webhookTimestamp`.

    Optional by construction: some payload shapes omit the field, and its absence
    must not itself be a reason to refuse an otherwise validly-signed delivery.
    Linear sends milliseconds since the epoch.
    """
    raw_ts = payload.get("webhookTimestamp")
    if raw_ts is None:
        return
    try:
        ts_seconds = float(raw_ts) / 1000.0
    except (TypeError, ValueError):
        _log.warning("ignoring non-numeric webhookTimestamp=%r", raw_ts)
        return
    age = abs(time.time() - ts_seconds)
    if age > _MAX_DELIVERY_AGE_SECONDS:
        raise HTTPException(
            status_code=401,
            detail=f"stale Linear webhook delivery ({age:.0f}s old)",
        )


@router.post("/webhook")
async def webhook(request: Request) -> dict[str, Any]:
    from agentconnect.linear.webhooks import handle_webhook

    # Fail closed before touching the body at all when unsigned webhooks would
    # otherwise be accepted.
    secret = _webhook_secret(request)

    # The raw bytes are what Linear actually signed — verify those, not a
    # round-tripped re-serialization of the parsed model (see `_verify_signature`).
    raw_body = await request.body()
    _verify_signature(secret, raw_body, request.headers.get("linear-signature"))

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")

    _reject_stale_delivery(payload)

    # NOTE on actor attribution: `webhooks._actor()` reads `data.user` /
    # `data.actor` from this payload. Before this fix that was a forgeable claim —
    # anyone reaching the port could name any actor. Now that the signature above
    # proves the payload came from Linear (not an arbitrary caller), that field is
    # Linear's own attribution of who commented/changed the issue, which is the
    # same trust Linear's UI itself extends to it. It is still not an
    # AgentConnect-authenticated *operator* identity — see `webhooks._actor` — so
    # the actions it drives (`approve_subtask`/`deny_subtask`/`complete_task`/
    # `request_review`) are recorded under that Linear-sourced name, not silently
    # upgraded to an operator principal.
    return {"results": handle_webhook(service(request), payload)}
