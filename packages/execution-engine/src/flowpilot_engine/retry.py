"""Exponential backoff with jitter and side-effect idempotency retry guard."""

from __future__ import annotations

import random
from dataclasses import dataclass

from flowpilot_schema import RetryPolicy, SideEffectPolicy


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    reason: str
    delay_ms: int = 0


def compute_backoff_ms(
    policy: RetryPolicy,
    attempt: int,
    *,
    rng: random.Random | None = None,
) -> int:
    """Compute exponential backoff delay in milliseconds with bounded symmetric jitter."""
    exponent = max(0, attempt - 1)
    base = min(
        float(policy.max_interval_ms),
        float(policy.initial_interval_ms) * (policy.backoff_multiplier**exponent),
    )
    if policy.jitter_ratio > 0.0:
        rand = rng if rng is not None else random.Random()
        spread = base * policy.jitter_ratio
        jittered = base + rand.uniform(-spread, spread)
        return max(10, round(min(float(policy.max_interval_ms), jittered)))
    return max(10, round(base))


def evaluate_retry(
    *,
    side_effect: SideEffectPolicy,
    policy: RetryPolicy,
    attempt: int,
    error_retryable: bool,
    idempotency_key: str | None,
    rng: random.Random | None = None,
) -> RetryDecision:
    """Decide whether a failed node attempt may be retried safely.

    Strictly forbids blindly retrying SideEffectPolicy.IRREVERSIBLE nodes unless
    allow_retry_with_idempotency_key=True AND a non-empty idempotency_key was resolved.
    """
    if not error_retryable:
        return RetryDecision(should_retry=False, reason="error is non-retryable")

    if attempt >= policy.max_attempts:
        return RetryDecision(
            should_retry=False,
            reason=f"max_attempts ({policy.max_attempts}) reached",
        )

    if side_effect == SideEffectPolicy.IRREVERSIBLE:
        if not policy.allow_retry_with_idempotency_key:
            return RetryDecision(
                should_retry=False,
                reason="irreversible side-effect node does not allow retries",
            )
        if not idempotency_key or not idempotency_key.strip():
            return RetryDecision(
                should_retry=False,
                reason="irreversible side-effect node requires a resolved idempotency key to retry",
            )

    delay_ms = compute_backoff_ms(policy, attempt, rng=rng)
    return RetryDecision(
        should_retry=True,
        reason="transient error eligible for retry",
        delay_ms=delay_ms,
    )
