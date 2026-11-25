"""Structured exceptions for FlowPilot node execution and orchestration signals."""

from __future__ import annotations

from typing import Any

from flowpilot_schema import RiskLevel


class FlowPilotNodeError(Exception):
    """Base structured error raised by FlowPilot nodes."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "NODE_EXECUTION_ERROR",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.details = details or {}


class RetryableNodeError(FlowPilotNodeError):
    """Transient failure eligible for policy-controlled retries (e.g. 503, network timeout)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "RETRYABLE_NODE_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, retryable=True, details=details)


class NonRetryableNodeError(FlowPilotNodeError):
    """Permanent failure that must not be retried (e.g. 400 validation error)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "NON_RETRYABLE_NODE_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, retryable=False, details=details)


class ConfigurationError(NonRetryableNodeError):
    """Raised when a node's resolved configuration violates its schema or contract."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="CONFIGURATION_ERROR", details=details)


class CredentialError(NonRetryableNodeError):
    """Raised when a required credential is missing, revoked, or unauthorized."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="CREDENTIAL_ERROR", details=details)


class SecurityPolicyError(NonRetryableNodeError):
    """Raised when a node violates egress, SSRF, tool allowlist, or budget policy."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "SECURITY_POLICY_VIOLATION",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class SideEffectIdempotencyError(NonRetryableNodeError):
    """Raised when retrying an irreversible side-effecting node without an idempotency key."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="SIDE_EFFECT_IDEMPOTENCY_ERROR", details=details)


class NodeTimeoutError(RetryableNodeError):
    """Raised when a node execution exceeds its configured timeout."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="NODE_TIMEOUT", details=details)


class ApprovalRequiredError(Exception):
    """Control signal raised by a node or policy gate to pause a run for human approval."""

    def __init__(
        self,
        *,
        title: str,
        description: str = "",
        risk_level: RiskLevel = RiskLevel.HIGH,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(title)
        self.title = title
        self.description = description
        self.risk_level = risk_level
        self.payload = payload or {}
