"""API key authentication, password hashing, and tenant authorization context."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_api.db.models import ApiKeyModel
from flowpilot_api.db.session import set_tenant_rls_context

API_KEY_PREFIX = "fp_live_"


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


@dataclass(frozen=True)
class TenantContext:
    organization_id: UUID
    actor: str
    scopes: frozenset[str]
    api_key_id: UUID | None = None


def _extract_api_key(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    query_key = request.query_params.get("api_key")
    if query_key:
        return query_key.strip()
    return None


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_tenant_context(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TenantContext:
    """Authenticate request via scoped API key and bind PostgreSQL RLS tenant context."""
    raw_key = _extract_api_key(request)
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide 'Authorization: Bearer <key>' or 'X-API-Key'.",
        )

    key_hash = hash_api_key(raw_key)
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
        api_key_id=row.id,
    )
