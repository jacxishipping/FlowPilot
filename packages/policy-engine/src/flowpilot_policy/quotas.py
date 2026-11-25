"""Subscription plan definitions, usage quota enforcement, and sliding-window rate limiter."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PlanTier(StrEnum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"
    ENTERPRISE = "enterprise"


class PlanLimits(BaseModel):
    """Monthly usage limits and pricing metadata per plan tier (Section 12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: PlanTier
    monthly_price_usd: int | None
    max_workflow_runs: int
    max_node_executions: int
    max_ai_tokens: int
    max_concurrency: int
    allow_premium_integrations: bool
    allow_overage: bool = False


PLAN_CATALOG: dict[PlanTier, PlanLimits] = {
    PlanTier.FREE: PlanLimits(
        tier=PlanTier.FREE,
        monthly_price_usd=0,
        max_workflow_runs=500,
        max_node_executions=5_000,
        max_ai_tokens=100_000,
        max_concurrency=2,
        allow_premium_integrations=False,
        allow_overage=False,
    ),
    PlanTier.STARTER: PlanLimits(
        tier=PlanTier.STARTER,
        monthly_price_usd=19,
        max_workflow_runs=5_000,
        max_node_executions=50_000,
        max_ai_tokens=1_000_000,
        max_concurrency=5,
        allow_premium_integrations=True,
        allow_overage=True,
    ),
    PlanTier.PRO: PlanLimits(
        tier=PlanTier.PRO,
        monthly_price_usd=49,
        max_workflow_runs=25_000,
        max_node_executions=250_000,
        max_ai_tokens=5_000_000,
        max_concurrency=15,
        allow_premium_integrations=True,
        allow_overage=True,
    ),
    PlanTier.BUSINESS: PlanLimits(
        tier=PlanTier.BUSINESS,
        monthly_price_usd=149,
        max_workflow_runs=100_000,
        max_node_executions=1_000_000,
        max_ai_tokens=25_000_000,
        max_concurrency=50,
        allow_premium_integrations=True,
        allow_overage=True,
    ),
    PlanTier.ENTERPRISE: PlanLimits(
        tier=PlanTier.ENTERPRISE,
        monthly_price_usd=None,
        max_workflow_runs=10_000_000,
        max_node_executions=100_000_000,
        max_ai_tokens=1_000_000_000,
        max_concurrency=500,
        allow_premium_integrations=True,
        allow_overage=True,
    ),
}


class QuotaEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    plan: PlanTier
    metric: str
    used: float
    limit: int
    warning: str | None = None
    in_overage: bool = False


def get_plan_limits(plan_name: str) -> PlanLimits:
    try:
        tier = PlanTier(plan_name.lower())
    except ValueError:
        tier = PlanTier.FREE
    return PLAN_CATALOG[tier]


def evaluate_quota(
    *,
    plan_name: str,
    metric: str,
    current_usage: float,
    requested_increment: float = 1.0,
) -> QuotaEvaluation:
    """Evaluate plan limits, 80% warnings, and overage rules for a usage metric."""
    limits = get_plan_limits(plan_name)
    metric_limits = {
        "workflow_run": limits.max_workflow_runs,
        "node_execution": limits.max_node_executions,
        "ai_tokens": limits.max_ai_tokens,
    }
    cap = metric_limits.get(metric, limits.max_node_executions)
    projected = current_usage + requested_increment

    if projected > cap:
        if limits.allow_overage:
            return QuotaEvaluation(
                allowed=True,
                plan=limits.tier,
                metric=metric,
                used=current_usage,
                limit=cap,
                warning=f"Usage for '{metric}' exceeded plan allowance ({cap}); overage applies.",
                in_overage=True,
            )
        return QuotaEvaluation(
            allowed=False,
            plan=limits.tier,
            metric=metric,
            used=current_usage,
            limit=cap,
            warning=f"Plan '{limits.tier.value}' quota exceeded for '{metric}' ({cap}).",
            in_overage=False,
        )

    warning = None
    if cap > 0 and (projected / cap) >= 0.8:
        pct = int((projected / cap) * 100)
        warning = f"Approaching plan limit for '{metric}': {pct}% used ({int(projected)}/{cap})."

    return QuotaEvaluation(
        allowed=True,
        plan=limits.tier,
        metric=metric,
        used=current_usage,
        limit=cap,
        warning=warning,
        in_overage=False,
    )


class TenantRateLimiter:
    """In-memory sliding-window tenant-aware rate limiter."""

    def __init__(self, *, max_requests: int = 60, window_seconds: float = 60.0) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.monotonic()
        cutoff = ts - self._window_seconds
        bucket = self._buckets[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._max_requests:
            return False
        bucket.append(ts)
        return True
