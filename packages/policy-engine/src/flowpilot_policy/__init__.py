"""FlowPilot RBAC, quota, and tenant rate-limiting policy engine."""

from flowpilot_policy.quotas import (
    PLAN_CATALOG,
    PlanLimits,
    PlanTier,
    QuotaEvaluation,
    TenantRateLimiter,
    evaluate_quota,
    get_plan_limits,
)
from flowpilot_policy.rbac import (
    ROLE_PERMISSIONS,
    PolicyDeniedError,
    Role,
    authorize,
    has_scope,
)
from flowpilot_policy.totp import (
    build_otpauth_uri,
    generate_totp_code,
    generate_totp_secret,
    verify_totp_code,
)

__all__ = [
    "PLAN_CATALOG",
    "ROLE_PERMISSIONS",
    "PlanLimits",
    "PlanTier",
    "PolicyDeniedError",
    "QuotaEvaluation",
    "Role",
    "TenantRateLimiter",
    "authorize",
    "build_otpauth_uri",
    "evaluate_quota",
    "generate_totp_code",
    "generate_totp_secret",
    "get_plan_limits",
    "has_scope",
    "verify_totp_code",
]
