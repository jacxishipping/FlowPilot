"""API key + HMAC-SHA256 browser JWT session authentication, RBAC enforcement, and RLS binding."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_api.db.models import ApiKeyModel
from flowpilot_api.db.session import set_tenant_rls_context
from flowpilot_policy import PolicyDeniedError, Role, authorize

API_KEY_PREFIX = "fp_live_"
SESSION_COOKIE_NAME = "flowpilot_session"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Generate (raw_key, key_prefix, key_hash)."""
    token = secrets.token_urlsafe(32)
    raw = f"{API_KEY_PREFIX}{token}"
    return raw, raw[:14], hash_api_key(raw)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _, salt, expected_hex = stored_hash.split("$", 2)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(digest.hex(), expected_hex)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_session_token(
    *,
    secret: str,
    user_id: UUID,
    email: str,
    organization_id: UUID,
    role: Role,
    ttl_seconds: int = 86_400,
) -> str:
    """Create a compact HS256 JWT session token."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    now = int(time.time())
    payload_dict = {
        "sub": str(user_id),
        "email": email,
        "org_id": str(organization_id),
        "role": role.value,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    payload = _b64url_encode(
        json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url_encode(sig)}"


def verify_session_token(token: str, *, secret: str) -> dict[str, Any] | None:
    """Verify HS256 JWT signature and expiration; return claims dict or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected_sig = _b64url_encode(
        hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    if not hmac.compare_digest(expected_sig, sig_b64):
        return None
    try:
        claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    if int(claims.get("exp", 0)) < int(time.time()):
        return None
    return claims


@dataclass(frozen=True)
class TenantContext:
    organization_id: UUID
    actor: str
    scopes: frozenset[str]
    role: Role | None = None
    user_id: UUID | None = None
    api_key_id: UUID | None = None


def _extract_bearer_or_key(request: Request) -> tuple[str | None, str | None]:
    """Return (raw_token, source) where source is 'header' | 'cookie' | 'query'."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip(), "header"
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip(), "header"
    cookie_tok = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_tok:
        return cookie_tok.strip(), "cookie"
    query_key = request.query_params.get("api_key")
    if query_key:
        return query_key.strip(), "query"
    return None, None


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_tenant_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TenantContext:
    """Authenticate via scoped API key OR signed browser JWT session and bind PG RLS."""
    raw_token, _source = _extract_bearer_or_key(request)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials. Provide Bearer token, X-API-Key, or session cookie.",
        )

    # 1. Scoped API key path
    if raw_token.startswith(API_KEY_PREFIX):
        key_hash = hash_api_key(raw_token)
        stmt = select(ApiKeyModel).where(
            ApiKeyModel.key_hash == key_hash,
            ApiKeyModel.revoked.is_(False),
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
            )
        await set_tenant_rls_context(session, row.organization_id)
        return TenantContext(
            organization_id=row.organization_id,
            actor=f"api_key:{row.key_prefix}",
            scopes=frozenset(row.scopes),
            role=None,
            api_key_id=row.id,
        )

    # 2. Signed browser JWT session path
    claims = verify_session_token(raw_token, secret=request.app.state.settings.session_secret)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )

    try:
        org_id = UUID(str(claims["org_id"]))
        user_id = UUID(str(claims["sub"]))
        role = Role(str(claims["role"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed session claims.",
        ) from exc

    await set_tenant_rls_context(session, org_id)
    return TenantContext(
        organization_id=org_id,
        actor=str(claims.get("email", user_id)),
        scopes=frozenset(),
        role=role,
        user_id=user_id,
    )


def require_permission(permission: str) -> Callable[..., Any]:
    """FastAPI dependency enforcing RBAC role permissions and API key scopes."""

    async def _checker(tenant: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        try:
            authorize(
                role=tenant.role,
                scopes=tenant.scopes,
                required_permission=permission,
            )
        except PolicyDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        return tenant

    return _checker
