"""Tenant-scoped agent memory store with vector cosine similarity (pgvector-compatible)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True)
class MemoryRecord:
    id: UUID
    organization_id: UUID
    agent_id: UUID
    content: str
    embedding: tuple[float, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


def deterministic_embed(text: str, *, dims: int = 32) -> tuple[float, ...]:
    """Lightweight deterministic hashing embedding for local/test vector recall."""
    vec = [0.0] * dims
    for token in text.lower().split():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        idx = digest[0] % dims
        sign = 1.0 if digest[1] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return tuple(v / norm for v in vec)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class AgentMemoryStore(Protocol):
    async def remember(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord: ...

    async def recall(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]: ...


class InMemoryVectorMemoryStore:
    """Tenant-isolated vector memory store enforcing organization_id scoping."""

    def __init__(self) -> None:
        self._records: list[MemoryRecord] = []

    async def remember(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        rec = MemoryRecord(
            id=uuid4(),
            organization_id=organization_id,
            agent_id=agent_id,
            content=content,
            embedding=deterministic_embed(content),
            metadata=metadata or {},
        )
        self._records.append(rec)
        return rec

    async def recall(
        self,
        *,
        organization_id: UUID,
        agent_id: UUID,
        query: str,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        q_vec = deterministic_embed(query)
        scoped = [
            rec
            for rec in self._records
            if rec.organization_id == organization_id and rec.agent_id == agent_id
        ]
        ranked = sorted(
            scoped,
            key=lambda r: cosine_similarity(q_vec, r.embedding),
            reverse=True,
        )
        return ranked[:limit]
