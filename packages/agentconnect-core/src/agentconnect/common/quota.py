"""Quota reservation & reconciliation (handoff §15).

Prevents concurrent agents from over-consuming a shared free-tier quota by
reserving capacity *before* the provider call and reconciling actual usage
*after*. Reservations are held in-process (fast, deterministic) and committed
usage is persisted to shared memory so limits survive across tasks and daily
resets.

Flow (§15):
    1. estimate -> 2. check -> 3. reserve -> 4. call -> 5. reconcile -> 6. release
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .config import ProviderConfig
from .memory import SharedMemory
from .schemas import QuotaReservation


def _day_start(now: float) -> float:
    """Epoch seconds for the start of the UTC day containing `now`."""
    return now - (now % 86400)


def _window_start(now: float, quota: dict) -> float:
    """Epoch seconds for the start of the quota accounting window.

    Free-tier providers use the default UTC-day boundary (``reset: daily`` or no
    ``window_seconds``) — identical to :func:`_day_start`, so existing configs are
    unchanged. Subscription / coding-plan providers (GLM, Kimi, MiniMax, Qwen)
    configure an explicit window, since their flat-fee quota resets on a period
    other than the UTC day:

    * ``reset: rolling`` + ``window_seconds`` — a sliding ``now - window`` window
      (e.g. a 5-hour prompt-usage window).
    * ``reset: calendar`` (or any non-rolling value) + ``window_seconds`` — fixed
      windows aligned to ``window_seconds`` boundaries (the daily default,
      generalized to an arbitrary length).

    Determinism is preserved: the window boundary is a pure function of ``now``
    and the config, with no hidden clock beyond the one the caller passes in.
    """
    window = quota.get("window_seconds")
    if window is None:
        return _day_start(now)
    window = float(window)
    if window <= 0:
        return _day_start(now)
    if quota.get("reset") == "rolling":
        return now - window
    return now - (now % window)


@dataclass
class _LiveReservation:
    reservation_id: str
    provider: str
    task_id: str
    requests: int
    tokens: int
    est_cost_usd: float
    expires_at: float


@dataclass
class QuotaLedger:
    """Tracks reservations + committed usage against per-provider quota rules."""

    memory: SharedMemory
    _reservations: dict[str, _LiveReservation] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --------------------------------------------------------------- helpers
    def _expire(self, now: float) -> None:
        dead = [rid for rid, r in self._reservations.items() if r.expires_at <= now]
        for rid in dead:
            del self._reservations[rid]

    def _reserved_totals(self, provider: str) -> tuple[int, int, float]:
        req = sum(r.requests for r in self._reservations.values() if r.provider == provider)
        tok = sum(r.tokens for r in self._reservations.values() if r.provider == provider)
        cost = sum(r.est_cost_usd for r in self._reservations.values() if r.provider == provider)
        return req, tok, cost

    @staticmethod
    def estimate_cost_usd(cfg: ProviderConfig, in_tokens: int, out_tokens: int) -> float:
        q = cfg.quota
        pin = q.get("price_per_1k_input_usd", 0.0)
        pout = q.get("price_per_1k_output_usd", 0.0)
        return (in_tokens / 1000.0) * pin + (out_tokens / 1000.0) * pout

    # ------------------------------------------------------------ public API
    def remaining(self, cfg: ProviderConfig, now: Optional[float] = None) -> dict[str, float]:
        """Remaining quota headroom for a provider, accounting for committed
        usage today AND outstanding reservations. Returns fractions in [0,1] plus
        absolute remainders. Local GPU providers are considered unlimited here
        (their admission is decided by the Local Model Manager)."""
        now = time.time() if now is None else now
        with self._lock:
            self._expire(now)
            res_req, res_tok, res_cost = self._reserved_totals(cfg.provider_id)
        q = cfg.quota
        kind = q.get("kind")
        if kind in (None, "local_gpu"):
            return {"kind": kind or "unknown", "unlimited": 1.0}

        used = self.memory.quota_usage_since(cfg.provider_id, _window_start(now, q))
        out: dict[str, float] = {}
        # Generic per-window caps (subscription/coding plans) with the legacy
        # ``max_daily_*`` names as backward-compatible aliases (free tiers).
        req_cap = q.get("max_requests", q.get("max_daily_requests"))
        if req_cap is not None:
            out["requests_remaining"] = max(0, req_cap - used["requests"] - res_req)
            out["requests_frac"] = out["requests_remaining"] / req_cap if req_cap else 0.0
        tok_cap = q.get("max_tokens", q.get("max_daily_tokens"))
        if tok_cap is not None:
            out["tokens_remaining"] = max(0, tok_cap - used["tokens"] - res_tok)
            out["tokens_frac"] = out["tokens_remaining"] / tok_cap if tok_cap else 0.0
        spend_cap = q.get("max_spend_usd", q.get("max_daily_spend_usd"))
        if spend_cap is not None:
            out["spend_remaining_usd"] = max(0.0, spend_cap - used["cost"] - res_cost)
            out["spend_frac"] = out["spend_remaining_usd"] / spend_cap if spend_cap else 0.0
        return out

    def can_reserve(self, cfg: ProviderConfig, in_tokens: int, out_tokens: int) -> tuple[bool, str]:
        rem = self.remaining(cfg)
        if rem.get("unlimited"):
            return True, "local_gpu_admission_delegated"
        tokens = in_tokens + out_tokens
        if "requests_remaining" in rem and rem["requests_remaining"] < 1:
            return False, "daily_request_quota_exhausted"
        if "tokens_remaining" in rem and rem["tokens_remaining"] < tokens:
            return False, "daily_token_quota_insufficient"
        if "spend_remaining_usd" in rem:
            cost = self.estimate_cost_usd(cfg, in_tokens, out_tokens)
            if rem["spend_remaining_usd"] < cost:
                return False, "daily_spend_budget_exhausted"
        return True, "capacity_available"

    def reserve(
        self, cfg: ProviderConfig, task_id: str, in_tokens: int, out_tokens: int, ttl: int = 120
    ) -> QuotaReservation:
        ok, reason = self.can_reserve(cfg, in_tokens, out_tokens)
        tokens = in_tokens + out_tokens
        cost = self.estimate_cost_usd(cfg, in_tokens, out_tokens)
        rid = f"resv_{uuid.uuid4().hex[:10]}"
        if not ok:
            return QuotaReservation(
                reservation_id=rid, provider=cfg.provider_id, task_id=task_id,
                estimated_input_tokens=in_tokens, estimated_output_tokens=out_tokens,
                requests=1, tokens=tokens, expires_in_seconds=ttl, granted=False, reason=reason,
            )
        now = time.time()
        with self._lock:
            self._reservations[rid] = _LiveReservation(
                reservation_id=rid, provider=cfg.provider_id, task_id=task_id,
                requests=1, tokens=tokens, est_cost_usd=cost, expires_at=now + ttl,
            )
        return QuotaReservation(
            reservation_id=rid, provider=cfg.provider_id, task_id=task_id,
            estimated_input_tokens=in_tokens, estimated_output_tokens=out_tokens,
            requests=1, tokens=tokens, expires_in_seconds=ttl, granted=True, reason=reason,
        )

    def reconcile(
        self,
        reservation: QuotaReservation,
        cfg: ProviderConfig,
        act_input: int,
        act_output: int,
        status: str = "completed",
        failure_reason: Optional[str] = None,
    ) -> None:
        """Commit actual usage to shared memory and release the reservation (§15)."""
        with self._lock:
            self._reservations.pop(reservation.reservation_id, None)
        self.memory.record_quota_usage(
            {
                "provider": cfg.provider_id,
                "task_id": reservation.task_id,
                "est_input": reservation.estimated_input_tokens,
                "est_output": reservation.estimated_output_tokens,
                "act_input": act_input,
                "act_output": act_output,
                "requests": 1 if status == "completed" else 0,
                "est_cost_usd": self.estimate_cost_usd(
                    cfg, reservation.estimated_input_tokens, reservation.estimated_output_tokens
                ),
                "act_cost_usd": self.estimate_cost_usd(cfg, act_input, act_output),
                "status": status,
                "failure_reason": failure_reason,
            }
        )

    def release(self, reservation: QuotaReservation) -> None:
        """Drop a reservation without committing usage (e.g. task cancelled pre-call)."""
        with self._lock:
            self._reservations.pop(reservation.reservation_id, None)

    # ------------------------------------------------------------ rented GPU
    # Rented nodes bill by TIME (hourly), not tokens. Budget comes from the
    # provider's `rental` config, and committed spend is persisted like cloud cost
    # so the daily cap survives across tasks (handoff Goal 4).
    def rental_remaining_usd(self, cfg: ProviderConfig, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        cap = cfg.rental.max_daily_usd if cfg.rental else 0.0
        if cap <= 0:
            return float("inf")  # no daily cap configured
        used = self.memory.quota_usage_since(cfg.provider_id, _day_start(now))
        return max(0.0, cap - used["cost"])

    def can_reserve_rental(self, cfg: ProviderConfig) -> tuple[bool, str]:
        """Whether a rental window can be afforded within the daily budget."""
        if cfg.rental is None:
            return False, "not_a_rented_node"
        rem = self.rental_remaining_usd(cfg)
        # Cost of the minimum billable window at the node's hourly rate.
        window_cost = cfg.rental.max_hourly_usd * (cfg.rental.min_rental_seconds / 3600.0)
        if rem < window_cost:
            return False, "daily_rental_budget_exhausted"
        return True, "rental_budget_available"

    def record_rental_window(
        self, cfg: ProviderConfig, task_id: str, seconds: float, status: str = "rented"
    ) -> float:
        """Commit the cost of a rental window (hourly rate * duration) to memory."""
        hourly = cfg.rental.max_hourly_usd if cfg.rental else 0.0
        cost = hourly * (max(0.0, seconds) / 3600.0)
        self.memory.record_quota_usage(
            {
                "provider": cfg.provider_id,
                "task_id": task_id,
                "est_input": 0,
                "est_output": 0,
                "act_input": 0,
                "act_output": 0,
                "requests": 1,
                "est_cost_usd": cost,
                "act_cost_usd": cost,
                "status": status,
                "failure_reason": None,
            }
        )
        return cost
