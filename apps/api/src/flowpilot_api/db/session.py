"""Async SQLAlchemy engine, session factory, and PostgreSQL RLS tenant context helper."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from flowpilot_api.config import normalize_async_database_url
from flowpilot_api.db.models import Base


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(normalize_async_database_url(database_url), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


async def init_schema(engine: AsyncEngine) -> None:
    """Create tables directly (used for local SQLite tests / bootstrap)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def set_tenant_rls_context(session: AsyncSession, organization_id: UUID) -> None:
    """Bind PostgreSQL session variable 'app.current_org_id' for Row-Level Security."""
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(organization_id)},
        )
