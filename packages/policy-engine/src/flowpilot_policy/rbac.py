"""RBAC roles, permission matrix, and scoped API key checks."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    OPERATOR = "operator"
    VIEWER = "viewer"


ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: frozenset({"*"}),
    Role.ADMIN: frozenset(
        {
            "org:read",
            "org:manage",
            "workflows:read",
            "workflows:write",
            "workflows:publish",
            "runs:read",
            "runs:execute",
            "runs:cancel",
            "approvals:decide",
            "credentials:read",
            "credentials:write",
            "agents:read",
            "agents:write",
            "agents:run",
            "billing:read",
            "billing:manage",
        }
    ),
    Role.EDITOR: frozenset(
        {
            "org:read",
            "workflows:read",
            "workflows:write",
            "workflows:publish",
            "runs:read",
            "runs:execute",
            "credentials:read",
            "agents:read",
            "agents:write",
            "agents:run",
        }
    ),
    Role.OPERATOR: frozenset(
        {
            "org:read",
            "workflows:read",
            "runs:read",
            "runs:execute",
            "runs:cancel",
            "approvals:decide",
            "agents:read",
            "agents:run",
        }
    ),
    Role.VIEWER: frozenset(
        {
            "org:read",
            "workflows:read",
            "runs:read",
            "agents:read",
            "billing:read",
        }
    ),
}


class PolicyDeniedError(PermissionError):
    """Raised when an actor lacks required role permissions or API key scopes."""

    def __init__(self, message: str, *, code: str = "POLICY_DENIED") -> None:
        super().__init__(message)
        self.code = code


def has_scope(granted_scopes: Iterable[str], required_scope: str) -> bool:
    """Check whether granted scopes satisfy required_scope (supports '*' and 'ns:*')."""
    scopes = set(granted_scopes)
    if "*" in scopes or required_scope in scopes:
        return True
    if ":" in required_scope:
        prefix = required_scope.split(":", 1)[0]
        if f"{prefix}:*" in scopes:
            return True
    return False


def authorize(
    *,
    role: Role | None = None,
    scopes: Iterable[str] = (),
    required_permission: str,
) -> None:
    """Enforce role and/or API key scope authorization."""
    if role is not None:
        role_perms = ROLE_PERMISSIONS[role]
        if not has_scope(role_perms, required_permission):
            raise PolicyDeniedError(
                f"Role '{role.value}' is not permitted to perform '{required_permission}'.",
                code="RBAC_DENIED",
            )
    scope_list = list(scopes)
    if scope_list and not has_scope(scope_list, required_permission):
        raise PolicyDeniedError(
            f"API key scopes do not include required scope '{required_permission}'.",
            code="SCOPE_DENIED",
        )
