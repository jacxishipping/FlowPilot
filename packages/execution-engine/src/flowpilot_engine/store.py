"""Persistence protocols, artifact payload externalization, and in-memory stores."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import UUID, uuid4

from flowpilot_schema import ArtifactReference, PayloadRef, WorkflowRun, WorkflowVersion


class ArtifactStore(Protocol):
    """Object-storage abstraction keeping large payloads out of orchestration history."""

    async def put_json(
        self,
        *,
        organization_id: UUID,
        run_id: UUID,
        payload: Any,
    ) -> ArtifactReference: ...

    async def get_json(self, ref: ArtifactReference) -> Any: ...


class InMemoryArtifactStore:
    """In-memory tenant-scoped artifact store."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put_json(
        self,
        *,
        organization_id: UUID,
        run_id: UUID,
        payload: Any,
    ) -> ArtifactReference:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        artifact_id = uuid4()
        uri = f"memory://tenants/{organization_id}/runs/{run_id}/artifacts/{artifact_id}.json"
        self._blobs[uri] = raw
        return ArtifactReference(
            artifact_id=artifact_id,
            uri=uri,
            content_type="application/json",
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    async def get_json(self, ref: ArtifactReference) -> Any:
        raw = self._blobs.get(ref.uri)
        if raw is None:
            raise KeyError(f"Artifact not found at '{ref.uri}'.")
        return json.loads(raw.decode("utf-8"))


class PayloadManager:
    """Stores small payloads inline and externalizes payloads over the byte threshold."""

    def __init__(
        self,
        artifact_store: ArtifactStore | None = None,
        *,
        inline_limit_bytes: int = 65_536,
    ) -> None:
        self._artifact_store = artifact_store or InMemoryArtifactStore()
        self._inline_limit_bytes = inline_limit_bytes

    async def pack(
        self,
        *,
        organization_id: UUID,
        run_id: UUID,
        payload: Any,
        inline_limit_bytes: int | None = None,
    ) -> PayloadRef:
        limit = inline_limit_bytes if inline_limit_bytes is not None else self._inline_limit_bytes
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(encoded) <= limit:
            return PayloadRef(inline=payload)
        ref = await self._artifact_store.put_json(
            organization_id=organization_id,
            run_id=run_id,
            payload=payload,
        )
        return PayloadRef(artifact=ref)

    async def unpack(self, ref: PayloadRef | None) -> Any:
        if ref is None:
            return None
        if ref.artifact is not None:
            return await self._artifact_store.get_json(ref.artifact)
        return ref.inline


class ExecutionStore(Protocol):
    """Repository protocol for workflow versions and durable workflow runs."""

    async def save_version(self, version: WorkflowVersion) -> None: ...
    async def get_version(self, version_id: UUID) -> WorkflowVersion | None: ...
    async def save_run(self, run: WorkflowRun) -> None: ...
    async def get_run(self, run_id: UUID) -> WorkflowRun | None: ...
    async def find_run_by_idempotency_key(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        idempotency_key: str,
    ) -> WorkflowRun | None: ...


class InMemoryExecutionStore:
    """In-memory ExecutionStore implementing tenant-scoped idempotency lookup."""

    def __init__(self) -> None:
        self._versions: dict[UUID, WorkflowVersion] = {}
        self._runs: dict[UUID, WorkflowRun] = {}
        self._idempotency_index: dict[tuple[UUID, UUID, str], UUID] = {}

    async def save_version(self, version: WorkflowVersion) -> None:
        self._versions[version.id] = version

    async def get_version(self, version_id: UUID) -> WorkflowVersion | None:
        return self._versions.get(version_id)

    async def save_run(self, run: WorkflowRun) -> None:
        self._runs[run.id] = run.model_copy(deep=True)
        if run.idempotency_key:
            key = (run.organization_id, run.workflow_id, run.idempotency_key)
            self._idempotency_index.setdefault(key, run.id)

    async def get_run(self, run_id: UUID) -> WorkflowRun | None:
        run = self._runs.get(run_id)
        return run.model_copy(deep=True) if run is not None else None

    async def find_run_by_idempotency_key(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        idempotency_key: str,
    ) -> WorkflowRun | None:
        run_id = self._idempotency_index.get((organization_id, workflow_id, idempotency_key))
        if run_id is None:
            return None
        return await self.get_run(run_id)
