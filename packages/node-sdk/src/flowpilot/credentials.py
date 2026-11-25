"""Credential declarations, secret wrappers, resolution protocol, and log redaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

REDACTED_PLACEHOLDER = "[REDACTED]"


class CredentialRequirement(BaseModel):
    """Declarative credential requirement exposed by a node definition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: str = Field(..., min_length=1, max_length=64)
    credential_type: str = Field(..., min_length=1, max_length=64)
    required: bool = True
    scopes: tuple[str, ...] = Field(default_factory=tuple)
    description: str = ""


class SecretValue:
    """Wrapper preventing accidental plaintext leakage of secrets in repr/str/logs."""

    __slots__ = ("_raw",)

    def __init__(self, raw: str) -> None:
        self._raw = raw

    def reveal(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"SecretValue({REDACTED_PLACEHOLDER!r})"

    def __str__(self) -> str:
        return REDACTED_PLACEHOLDER

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._raw == other._raw
        return False


@dataclass(frozen=True)
class ResolvedCredential:
    """Tenant-scoped decrypted credential payload passed to a node at runtime."""

    credential_id: UUID
    organization_id: UUID
    credential_type: str
    secrets: dict[str, SecretValue] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    scopes: tuple[str, ...] = ()

    def get_secret(self, key: str) -> str:
        if key not in self.secrets:
            raise KeyError(f"Secret key '{key}' not present in credential '{self.credential_id}'.")
        return self.secrets[key].reveal()

    def raw_secret_values(self) -> list[str]:
        return [s.reveal() for s in self.secrets.values() if s.reveal()]


class CredentialResolver(Protocol):
    """Protocol implemented by the encrypted credential manager to resolve tenant credentials."""

    async def resolve(
        self,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> ResolvedCredential: ...


def redact_text(text: str, secrets: list[str]) -> str:
    """Scrub all known secret strings from text."""
    result = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            result = result.replace(secret, REDACTED_PLACEHOLDER)
    return result


def redact_value(value: Any, secrets: list[str]) -> Any:
    """Recursively scrub secrets from strings, lists, and dicts."""
    if not secrets:
        return value
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {k: redact_value(v, secrets) for k, v in value.items()}
    return value
