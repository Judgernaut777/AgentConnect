"""Push notifiers for spend approvals (ntfy / Slack / Discord / raw webhook).

When a paid/rented charge (or a budget request) is pending, a notifier pushes it to
the user's phone/chat so they don't have to watch the dashboard. Each takes an
approval item and a set of links and POSTs a service-shaped payload.

Inline actions:
  * ntfy — TRUE one-tap Approve/Deny: ntfy renders HTTP action buttons and the ntfy
    app POSTs directly to the approve/deny endpoints (requires the approval server to
    be reachable from the phone — set AGENTCONNECT_APPROVAL_URL to a public/tunnel URL).
  * Slack / Discord — incoming webhooks can't do interactive callbacks (that needs a
    full app), so they get a rich message with a one-tap **link** to the per-item action
    page (`/a/{id}`), where the user confirms. Safe (no GET side effects).

Deliberately stdlib-only (urllib). ``post_fn`` is injectable for offline tests.
"""

from __future__ import annotations

import abc
import json
import os
import urllib.request
from typing import Callable, Optional

PostFn = Callable[[str, bytes, dict], None]


def _urllib_post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=10)  # best effort; caller swallows errors


class Notifier(abc.ABC):
    def __init__(self, post_fn: Optional[PostFn] = None):
        self._post = post_fn or _urllib_post

    @abc.abstractmethod
    def send(self, item: dict, links: dict) -> None:
        """Push a pending approval. `item` is ApprovalQueue.to_public(); `links` has
        keys dashboard/item_page/approve/deny."""


class RawWebhookNotifier(Notifier):
    """Generic JSON POST (back-compat with the original single-webhook behavior)."""

    def __init__(self, url: str, post_fn: Optional[PostFn] = None):
        super().__init__(post_fn)
        self._url = url

    def send(self, item: dict, links: dict) -> None:
        body = json.dumps({**item, **{f"{k}_url": v for k, v in links.items()}}).encode()
        self._post(self._url, body, {"Content-Type": "application/json"})


class NtfyNotifier(Notifier):
    """ntfy.sh (or self-hosted). Charges get one-tap POST Approve/Deny buttons."""

    def __init__(self, topic_url: str, post_fn: Optional[PostFn] = None, priority: str = "high"):
        super().__init__(post_fn)
        self._url = topic_url
        self._priority = priority

    def send(self, item: dict, links: dict) -> None:
        if item.get("kind") == "charge":
            actions = (
                f"http, Approve, {links['approve']}, method=POST, clear=true; "
                f"http, Deny, {links['deny']}, method=POST, clear=true"
            )
            tags = "money_with_wings"
        else:
            actions = f"view, Set budget, {links['item_page']}, clear=true"
            tags = "moneybag"
        headers = {
            "Title": "AgentConnect spend approval",
            "Priority": self._priority,
            "Tags": tags,
            "Actions": actions,
        }
        self._post(self._url, item.get("text", "").encode(), headers)


class SlackNotifier(Notifier):
    """Slack incoming webhook: rich message + a link button to the action page."""

    def __init__(self, webhook_url: str, post_fn: Optional[PostFn] = None):
        super().__init__(post_fn)
        self._url = webhook_url

    def send(self, item: dict, links: dict) -> None:
        payload = {
            "text": f":money_with_wings: {item.get('text', '')}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": item.get("text", "")}},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review & approve"},
                            "url": links["item_page"],
                            "style": "primary",
                        }
                    ],
                },
            ],
        }
        self._post(self._url, json.dumps(payload).encode(), {"Content-Type": "application/json"})


class DiscordNotifier(Notifier):
    """Discord incoming webhook: an embed + a link to the action page."""

    def __init__(self, webhook_url: str, post_fn: Optional[PostFn] = None):
        super().__init__(post_fn)
        self._url = webhook_url

    def send(self, item: dict, links: dict) -> None:
        payload = {
            "embeds": [
                {
                    "title": "AgentConnect spend approval",
                    "description": f"{item.get('text', '')}\n\n[Review & approve]({links['item_page']})",
                    "color": 15105570,
                }
            ]
        }
        self._post(self._url, json.dumps(payload).encode(), {"Content-Type": "application/json"})


class MultiNotifier(Notifier):
    """Fan out to several notifiers; one failing never blocks the others."""

    def __init__(self, notifiers: list[Notifier]):
        super().__init__(None)
        self._notifiers = notifiers

    def send(self, item: dict, links: dict) -> None:
        for n in self._notifiers:
            try:
                n.send(item, links)
            except Exception:  # noqa: BLE001 — a bad notifier must not break the rest
                continue


def notifier_from_env(post_fn: Optional[PostFn] = None) -> Optional[Notifier]:
    """Build a notifier from env. AGENTCONNECT_NOTIFY is a comma-separated list of
    ntfy|slack|discord|webhook; each needs its URL env var. Returns None if none set."""
    modes = [m.strip().lower() for m in os.environ.get("AGENTCONNECT_NOTIFY", "").split(",") if m.strip()]
    built: list[Notifier] = []
    for m in modes:
        if m == "ntfy" and os.environ.get("AGENTCONNECT_NTFY_URL"):
            built.append(NtfyNotifier(os.environ["AGENTCONNECT_NTFY_URL"], post_fn))
        elif m == "slack" and os.environ.get("AGENTCONNECT_SLACK_WEBHOOK"):
            built.append(SlackNotifier(os.environ["AGENTCONNECT_SLACK_WEBHOOK"], post_fn))
        elif m == "discord" and os.environ.get("AGENTCONNECT_DISCORD_WEBHOOK"):
            built.append(DiscordNotifier(os.environ["AGENTCONNECT_DISCORD_WEBHOOK"], post_fn))
        elif m == "webhook" and os.environ.get("AGENTCONNECT_APPROVAL_WEBHOOK"):
            built.append(RawWebhookNotifier(os.environ["AGENTCONNECT_APPROVAL_WEBHOOK"], post_fn))
    # Back-compat: a bare AGENTCONNECT_APPROVAL_WEBHOOK with no AGENTCONNECT_NOTIFY.
    if not built and os.environ.get("AGENTCONNECT_APPROVAL_WEBHOOK"):
        built.append(RawWebhookNotifier(os.environ["AGENTCONNECT_APPROVAL_WEBHOOK"], post_fn))
    if not built:
        return None
    return built[0] if len(built) == 1 else MultiNotifier(built)
