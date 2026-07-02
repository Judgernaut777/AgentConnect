"""Push notifiers for spend approvals — payload shapes and the env factory, driven
offline via an injected post_fn (no real network)."""

import json

from agentconnect.common.notifiers import (
    DiscordNotifier,
    MultiNotifier,
    NtfyNotifier,
    RawWebhookNotifier,
    SlackNotifier,
    notifier_from_env,
)

ITEM = {"id": "appr_1", "kind": "charge", "text": "Approve ~$0.02 to openai_paid",
        "provider": "openai_paid", "estimated_cost_usd": 0.02}
BUDGET_ITEM = {"id": "appr_2", "kind": "budget", "text": "Set a spend budget",
               "suggested_period": "monthly"}
LINKS = {"dashboard": "http://h/", "item_page": "http://h/a/appr_1",
         "approve": "http://h/api/charges/appr_1/approve", "deny": "http://h/api/charges/appr_1/deny"}


def _capture():
    calls = []
    return calls, lambda url, data, headers: calls.append((url, data, headers))


def test_ntfy_charge_has_one_tap_post_actions():
    calls, post = _capture()
    NtfyNotifier("https://ntfy.sh/mytopic", post_fn=post).send(ITEM, LINKS)
    url, data, headers = calls[0]
    assert url == "https://ntfy.sh/mytopic"
    assert headers["Actions"].count("method=POST") == 2
    assert LINKS["approve"] in headers["Actions"] and LINKS["deny"] in headers["Actions"]
    assert data == ITEM["text"].encode()


def test_ntfy_budget_uses_view_action():
    calls, post = _capture()
    NtfyNotifier("https://ntfy.sh/t", post_fn=post).send(BUDGET_ITEM, LINKS)
    _, _, headers = calls[0]
    assert "view" in headers["Actions"] and LINKS["item_page"] in headers["Actions"]


def test_slack_has_link_button_to_item_page():
    calls, post = _capture()
    SlackNotifier("https://hooks.slack.com/x", post_fn=post).send(ITEM, LINKS)
    url, data, headers = calls[0]
    payload = json.loads(data)
    btn = payload["blocks"][1]["elements"][0]
    assert btn["url"] == LINKS["item_page"] and headers["Content-Type"] == "application/json"


def test_discord_embed_links_to_item_page():
    calls, post = _capture()
    DiscordNotifier("https://discord.com/api/webhooks/x", post_fn=post).send(ITEM, LINKS)
    payload = json.loads(calls[0][1])
    assert LINKS["item_page"] in payload["embeds"][0]["description"]


def test_raw_webhook_includes_urls():
    calls, post = _capture()
    RawWebhookNotifier("https://example.com/hook", post_fn=post).send(ITEM, LINKS)
    payload = json.loads(calls[0][1])
    assert payload["approve_url"] == LINKS["approve"] and payload["id"] == "appr_1"


def test_multi_notifier_fans_out_and_isolates_failures():
    calls, post = _capture()

    class Boom(RawWebhookNotifier):
        def send(self, item, links):
            raise RuntimeError("down")

    multi = MultiNotifier([Boom("x", post_fn=post), SlackNotifier("s", post_fn=post)])
    multi.send(ITEM, LINKS)  # must not raise
    assert len(calls) == 1  # slack still fired despite the boom notifier


def test_notifier_from_env_selects_and_combines(monkeypatch):
    monkeypatch.setenv("AGENTCONNECT_NOTIFY", "ntfy,slack")
    monkeypatch.setenv("AGENTCONNECT_NTFY_URL", "https://ntfy.sh/t")
    monkeypatch.setenv("AGENTCONNECT_SLACK_WEBHOOK", "https://hooks.slack.com/x")
    n = notifier_from_env()
    assert isinstance(n, MultiNotifier)

    monkeypatch.delenv("AGENTCONNECT_NOTIFY")
    monkeypatch.setenv("AGENTCONNECT_APPROVAL_WEBHOOK", "https://example.com/hook")
    assert isinstance(notifier_from_env(), RawWebhookNotifier)  # back-compat


def test_authorizer_uses_notifier(monkeypatch):
    # WebApprovalAuthorizer should push through the notifier when one is set.
    from agentconnect.common.approval import ApprovalQueue, WebApprovalAuthorizer
    from agentconnect.common.authorization import ChargeRequest

    calls, post = _capture()
    q = ApprovalQueue()
    auth = WebApprovalAuthorizer(q, notifier=NtfyNotifier("https://ntfy.sh/t", post_fn=post),
                                 timeout_seconds=0.05)
    auth.confirm_charge(ChargeRequest("openai_paid", "paid_cloud", 0.02, "x"))  # times out -> deny
    # The notify thread fired the push (poll briefly for the daemon thread).
    import time
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls and calls[0][0] == "https://ntfy.sh/t"
