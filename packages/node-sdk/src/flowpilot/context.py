"""Execution context, secret-redacting logger, and usage metering helpers for nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from flowpilot.credentials import (
    CredentialResolver,
    ResolvedCredential,
    redact_text,
    redact_value,
)
from flowpilot.errors import CredentialError
from flowpilot.http import ControlledHttpClient
from flowpilot_schema import ApprovalRequest, UsageRecord


@dataclass(frozen=True)
class LogEntry:
    """Structured log entry emitted by a node with secrets automatically redacted."""

    level: str
    message: str
    node_id: str
    timestamp: datetime
    fields: dict[str, Any] = field(default_factory=dict)


class ExecutionContext:
    """Runtime context injected into every BaseNode.execute() call."""

    def __init__(
        self,
        *,
        run_id: UUID,
        organization_id: UUID,
        workflow_id: UUID,
        workflow_version_id: UUID,
        node_id: str,
        attempt: int = 1,
        idempotency_key: str | None = None,
        credential_bindings: dict[str, UUID] | None = None,
        credential_resolver: CredentialResolver | None = None,
        http_client: ControlledHttpClient | None = None,
        trigger_payload: dict[str, Any] | None = None,
        approval_resolution: ApprovalRequest | None = None,
        on_log: Callable[[LogEntry], None] | None = None,
        on_usage: Callable[[UsageRecord], None] | None = None,
        is_canceled: Callable[[], bool] | None = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.run_id = run_id
        self.organization_id = organization_id
        self.workflow_id = workflow_id
        self.workflow_version_id = workflow_version_id
        self.node_id = node_id
        self.attempt = attempt
        self.idempotency_key = idempotency_key
        self._credential_bindings = dict(credential_bindings or {})
        self._credential_resolver = credential_resolver
        self.http = http_client or ControlledHttpClient()
        self.trigger_payload = dict(trigger_payload or {})
        self.approval_resolution = approval_resolution
        self._on_log = on_log
        self._on_usage = on_usage
        self._is_canceled = is_canceled
        self.services = dict(services or {})
        self._known_secrets: list[str] = []
        self.logs: list[LogEntry] = []
        self.usage_records: list[UsageRecord] = []

    def register_secret_for_redaction(self, secret: str) -> None:
        if secret and secret not in self._known_secrets:
            self._known_secrets.append(secret)

    async def get_credential(self, slot: str) -> ResolvedCredential:
        """Resolve an encrypted tenant credential bound to the given node slot."""
        if slot not in self._credential_bindings:
            raise CredentialError(
                f"Credential slot '{slot}' is not bound on node '{self.node_id}'."
            )
        if self._credential_resolver is None:
            raise CredentialError("No credential resolver configured in execution context.")

        credential_id = self._credential_bindings[slot]
        resolved = await self._credential_resolver.resolve(
            organization_id=self.organization_id,
            credential_id=credential_id,
        )
        if resolved.organization_id != self.organization_id:
            raise CredentialError(
                f"Tenant isolation violation: credential '{credential_id}' does not belong "
                f"to organization '{self.organization_id}'."
            )
        for secret in resolved.raw_secret_values():
            self.register_secret_for_redaction(secret)
        return resolved

    def log(self, level: str, message: str, **fields: Any) -> LogEntry:
        """Emit a structured log line with all known secrets automatically redacted."""
        entry = LogEntry(
            level=level.upper(),
            message=redact_text(message, self._known_secrets),
            node_id=self.node_id,
            timestamp=datetime.now(UTC),
            fields=redact_value(fields, self._known_secrets),
        )
        self.logs.append(entry)
        if self._on_log is not None:
            self._on_log(entry)
        return entry

    def record_usage(
        self,
        metric: str,
        quantity: float,
        *,
        unit: str = "count",
        metadata: dict[str, Any] | None = None,
    ) -> UsageRecord:
        """Record billable/metered usage attributed to this tenant, run, and node."""
        record = UsageRecord(
            organization_id=self.organization_id,
            run_id=self.run_id,
            node_id=self.node_id,
            metric=metric,
            quantity=quantity,
            unit=unit,
            metadata=redact_value(metadata or {}, self._known_secrets),
        )
        self.usage_records.append(record)
        if self._on_usage is not None:
            self._on_usage(record)
        return record

    def check_canceled(self) -> bool:
        """Return True if the enclosing workflow run has received a cancellation signal."""
        return bool(self._is_canceled and self._is_canceled())

    def redact(self, value: Any) -> Any:
        """Redact any known secrets from an arbitrary string/dict/list."""
        return redact_value(value, self._known_secrets)
