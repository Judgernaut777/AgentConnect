import time

from agentconnect.common.config import ProviderConfig
from agentconnect.common.memory import SharedMemory
from agentconnect.common.quota import QuotaLedger, _day_start, _window_start


def _free_provider():
    return ProviderConfig(
        provider_id="gemini_free", type="cloud", endpoint="x", secret_ref="op://x",
        privacy="external", capabilities=("classification",),
        quota={"kind": "free_tier", "max_daily_requests": 2, "max_daily_tokens": 10000},
    )


def _paid_provider():
    return ProviderConfig(
        provider_id="openai_paid", type="cloud", endpoint="x", secret_ref="op://x",
        privacy="external_paid", capabilities=("hard_reasoning",),
        quota={"kind": "paid", "max_daily_spend_usd": 0.01,
               "price_per_1k_input_usd": 0.0025, "price_per_1k_output_usd": 0.01},
    )


def test_reservation_reduces_remaining_and_blocks_overspend():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = _free_provider()

    r1 = ledger.reserve(cfg, "t1", 100, 100)
    assert r1.granted
    r2 = ledger.reserve(cfg, "t2", 100, 100)
    assert r2.granted
    # third request exceeds the 2-request daily cap while both are reserved
    r3 = ledger.reserve(cfg, "t3", 100, 100)
    assert not r3.granted
    assert r3.reason == "daily_request_quota_exhausted"


def test_reconcile_persists_usage():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = _free_provider()
    r = ledger.reserve(cfg, "t1", 100, 50)
    ledger.reconcile(r, cfg, act_input=120, act_output=40)
    rem = ledger.remaining(cfg)
    assert rem["tokens_remaining"] == 10000 - 160
    assert rem["requests_remaining"] == 1


def test_paid_budget_enforced():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = _paid_provider()
    # 1000 output tokens -> $0.01 exactly, at the cap; a bit more must be blocked.
    ok, reason = ledger.can_reserve(cfg, 0, 2000)
    assert not ok
    assert reason == "daily_spend_budget_exhausted"


def test_local_gpu_is_unlimited():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = ProviderConfig(
        provider_id="local_r9700", type="local", endpoint="x", secret_ref="op://x",
        privacy="local_only", capabilities=("coding",), quota={"kind": "local_gpu"},
    )
    ok, _ = ledger.can_reserve(cfg, 100000, 100000)
    assert ok


# --------------------------------------------------------------------------- #
# Subscription / coding-plan providers: flat-fee, windowed quota, $0 marginal.
# --------------------------------------------------------------------------- #
def _sub_provider(**quota_overrides):
    quota = {"kind": "subscription", "reset": "rolling", "window_seconds": 18000}
    quota.update(quota_overrides)
    return ProviderConfig(
        provider_id="glm_coding_plan", type="cloud", endpoint="x", secret_ref="op://x",
        privacy="external", capabilities=("coding",), quota=quota,
    )


def test_window_start_modes():
    now = 1_000_000.0
    # Legacy free-tier configs (no window) keep the exact UTC-day boundary.
    assert _window_start(now, {}) == _day_start(now)
    assert _window_start(now, {"reset": "daily"}) == _day_start(now)
    # A degenerate window falls back to daily rather than dividing by zero.
    assert _window_start(now, {"window_seconds": 0}) == _day_start(now)
    # Calendar-aligned window of arbitrary length (daily generalized).
    assert _window_start(now, {"window_seconds": 3600}) == now - (now % 3600)
    # Rolling window slides with `now`.
    assert _window_start(now, {"reset": "rolling", "window_seconds": 3600}) == now - 3600


def test_subscription_zero_marginal_cost():
    # No price_per_1k_* keys -> the router sees these as free-to-call while the
    # window has headroom (no cost penalty, no external_paid budget gate).
    cfg = _sub_provider(max_requests=100)
    assert QuotaLedger.estimate_cost_usd(cfg, 10_000, 10_000) == 0.0


def test_subscription_generic_cap_keys_enforced():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = _sub_provider(max_tokens=300)
    # 400 tokens requested against a 300-token window allowance -> blocked.
    ok, reason = ledger.can_reserve(cfg, 200, 200)
    assert not ok
    assert reason == "daily_token_quota_insufficient"
    # A request that fits is admitted.
    ok2, _ = ledger.can_reserve(cfg, 100, 100)
    assert ok2


def test_subscription_rolling_window_expires_old_usage():
    mem = SharedMemory()
    ledger = QuotaLedger(memory=mem)
    cfg = _sub_provider(max_requests=2)  # 5h rolling window, 2 requests

    for tid in ("t1", "t2"):
        r = ledger.reserve(cfg, tid, 100, 100)
        assert r.granted
        ledger.reconcile(r, cfg, 100, 100)

    # Window is now full -> the router would drop this provider and fall through.
    assert ledger.remaining(cfg)["requests_remaining"] == 0
    assert ledger.can_reserve(cfg, 100, 100) == (False, "daily_request_quota_exhausted")

    # Once the 5h window has elapsed, the earlier usage slides out and the full
    # allowance is available again.
    later = time.time() + 18000 + 60
    assert ledger.remaining(cfg, now=later)["requests_remaining"] == 2
