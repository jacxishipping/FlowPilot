"""SQLAlchemy / PostgreSQL pgvector-backed tenant-scoped AgentMemoryStore."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Text, Uuid, column, insert, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flowpilot_agents.memory import (
    MemoryRecord,
    cosine_similarity,
    deterministic_embed,
)

_AGENT_MEMORY_TABLE = table(
    "agent_memory",
    column("id", Uuid(as_uuid=True)),
    column("organization_id", Uuid(as_uuid=True)),
    column("agent_id", Uuid(as_uuid=True)),
    column("content", Text),
    column("metadata_json", JSON),
    column("created_at", DateTime(timezone=True)),
)


class SqlAlchemyVectorMemoryStore:
    """Tenant-scoped agent memory store compatible with PostgreSQL pgvector + RLS and SQLite."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        embedding_dims: int = 32,
    ) -> None:
        self._session_factory = session_factory
        self._dims = embedding_dims

    async def remember(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        rec_id = uuid4()
        vec = deterministic_embed(content, dims=self._dims)
        meta = dict(metadata or {})
        meta["_embedding"] = list(vec)

        async with self._session_factory() as session:
            bind = session.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :org_id, true)"),
                    {"org_id": str(organization_id)},
                )
            await session.execute(
                insert(_AGENT_MEMORY_TABLE).values(
                    id=rec_id,
                    organization_id=organization_id,
                    agent_id=agent_id,
                    content=content,
                    metadata_json=meta,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()

        return MemoryRecord(
            id=rec_id,
            organization_id=organization_id,
            agent_id=agent_id,
            content=content,
            embedding=vec,
            metadata=metadata or {},
        )

    async def recall(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        q_vec = deterministic_embed(query, dims=self._dims)
        async with self._session_factory() as session:
            bind = session.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                await session.execute(
                    text("SELECT set_config('app.current_org_id', :org_id, true)"),
                    {"org_id": str(organization_id)},
                )

            stmt = select(_AGENT_MEMORY_TABLE).where(
                _AGENT_MEMORY_TABLE.c.organization_id == organization_id,
                _AGENT_MEMORY_TABLE.c.agent_id == agent_id,
            )
            rows = (await session.execute(stmt)).mappings().all()

        records: list[MemoryRecord] = []
        for row in rows:
            raw_meta = dict(row["metadata_json"] or {})
            stored_emb = raw_meta.pop("_embedding", None)
            emb = (
                tuple(float(v) for v in stored_emb)
                if isinstance(stored_emb, list)
                else deterministic_embed(str(row["content"]), dims=self._dims)
            )
            records.append(
                MemoryRecord(
                    id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
                    organization_id=organization_id,
                    agent_id=agent_id,
                    content=str(row["content"]),
                    embedding=emb,
                    metadata=raw_meta,
                )
            )

        ranked = sorted(
            records,
            key=lambda r: cosine_similarity(q_vec, r.embedding),
            reverse=True,
        )
        return ranked[:limit]
