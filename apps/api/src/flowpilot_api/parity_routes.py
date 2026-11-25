"""Full n8n-parity API routes: Single-Node Test Step, Folders, Built-in Data Tables,
Binary Files, Knowledge Base RAG, Insights Analytics, Hosted ChatHub, Interactive AI Assistant,
and OpenAPI-to-Node dynamic connector import."""

from __future__ import annotations

import base64
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flowpilot import (
    ControlledHttpClient,
    ExecutionContext,
    FlowPilotNodeError,
    NodeRegistry,
    NodeResult,
    generate_nodes_from_openapi,
)
from flowpilot_agents import cosine_similarity, deterministic_embed
from flowpilot_api.auth import TenantContext, get_db_session, get_tenant_context, require_permission
from flowpilot_api.db.models import (
    BinaryFileModel,
    ChatMessageModel,
    ChatSessionModel,
    DataTableModel,
    DataTableRowModel,
    FolderModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    ProjectModel,
    UsageRecordModel,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowVersionModel,
)
from flowpilot_engine import WorkflowExecutionEngine
from flowpilot_expressions import ExpressionError, resolve_value
from flowpilot_schema import (
    ArtifactReference,
    EdgeDefinition,
    NodeDefinition,
    NodeUI,
    Position,
    WorkflowDefinition,
    WorkflowGraph,
    WorkflowVersion,
)
from flowpilot_validator import WorkflowValidator

parity_router = APIRouter(prefix="/api/v1")


# --- Engine Service Factories (Data Tables & Knowledge RAG) ---


def build_data_table_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Create a tenant-isolated CRUD handler backing the core.data_table node."""

    async def _handle(
        organization_id: UUID,
        table_name: str,
        operation: str,
        data: dict[str, Any] | None,
        row_id: str | None,
    ) -> dict[str, Any]:
        async with session_factory() as session:
            tbl = (
                await session.execute(
                    select(DataTableModel).where(
                        DataTableModel.organization_id == organization_id,
                        DataTableModel.name == table_name,
                    )
                )
            ).scalar_one_or_none()
            if tbl is None:
                tbl = DataTableModel(
                    organization_id=organization_id,
                    name=table_name,
                    description="Auto-created by workflow Data Table node",
                )
                session.add(tbl)
                await session.flush()

            if operation == "insert":
                row = DataTableRowModel(
                    organization_id=organization_id,
                    table_id=tbl.id,
                    row_data=dict(data or {}),
                )
                session.add(row)
                await session.commit()
                return {"id": str(row.id), "table": tbl.name, "data": row.row_data}

            if operation == "update" and row_id:
                existing = await session.get(DataTableRowModel, UUID(row_id))
                if existing is None or existing.organization_id != organization_id:
                    return {"updated": False, "id": row_id}
                existing.row_data = {**existing.row_data, **dict(data or {})}
                await session.commit()
                return {"updated": True, "id": str(existing.id), "data": existing.row_data}

            if operation == "delete" and row_id:
                existing = await session.get(DataTableRowModel, UUID(row_id))
                if existing is not None and existing.organization_id == organization_id:
                    await session.delete(existing)
                    await session.commit()
                    return {"deleted": True, "id": row_id}
                return {"deleted": False, "id": row_id}

            rows = (
                (
                    await session.execute(
                        select(DataTableRowModel)
                        .where(
                            DataTableRowModel.organization_id == organization_id,
                            DataTableRowModel.table_id == tbl.id,
                        )
                        .order_by(DataTableRowModel.created_at.desc())
                        .limit(200)
                    )
                )
                .scalars()
                .all()
            )
            return {
                "table": tbl.name,
                "count": len(rows),
                "rows": [{"id": str(r.id), **r.row_data} for r in rows],
            }

    return _handle


def build_knowledge_search_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Create a tenant-isolated semantic RAG search handler backing core.knowledge_search."""

    async def _search(
        organization_id: UUID,
        query: str,
        top_k: int = 5,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        q_vec = deterministic_embed(query)
        async with session_factory() as session:
            stmt = select(KnowledgeChunkModel).where(
                KnowledgeChunkModel.organization_id == organization_id
            )
            if collection:
                stmt = stmt.where(KnowledgeChunkModel.collection == collection)
            chunks = (await session.execute(stmt)).scalars().all()
            scored: list[tuple[float, KnowledgeChunkModel]] = []
            for ch in chunks:
                vec = tuple(float(x) for x in ch.embedding_json) if ch.embedding_json else ()
                score = cosine_similarity(q_vec, vec) if len(vec) == len(q_vec) else 0.0
                if query.lower() in ch.content.lower():
                    score += 0.25
                scored.append((score, ch))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [
                {
                    "chunk_id": str(ch.id),
                    "document_id": str(ch.document_id),
                    "collection": ch.collection,
                    "score": round(score, 4),
                    "content": ch.content,
                }
                for score, ch in scored[:top_k]
            ]

    return _search


# --- 1. Single-Node Execution ("Test Step") ---


class SingleNodeTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(default="step", min_length=1, max_length=64)
    node_type: str = Field(..., min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, UUID] = Field(default_factory=dict)
    pinned_nodes: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)


@parity_router.post("/nodes/test")
async def test_single_node(
    body: SingleNodeTestRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
) -> dict[str, Any]:
    """Execute one node in isolation using pinned upstream outputs and real org credentials."""
    registry: NodeRegistry = request.app.state.registry
    node_cls = registry.get(body.node_type)
    if node_cls is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown node type '{body.node_type}'.",
        )

    expr_ctx = {
        "nodes": {nid: {"output": out} for nid, out in body.pinned_nodes.items()},
        "trigger": body.trigger,
        "inputs": body.trigger,
    }
    try:
        resolved_cfg = resolve_value(body.config, expr_ctx)
        validated_cfg = node_cls.config_model.model_validate(resolved_cfg)
    except (ExpressionError, ValueError) as exc:
        return {
            "ok": False,
            "node_id": body.node_id,
            "resolved_input": body.config,
            "output": None,
            "duration_ms": 0,
            "logs": [],
            "error": str(exc),
        }

    http_client = ControlledHttpClient(
        allow_private_networks=request.app.state.settings.allow_private_networks,
        transport=getattr(request.app.state.engine, "_http_transport", None),
    )
    ctx = ExecutionContext(
        run_id=uuid4(),
        organization_id=tenant.organization_id,
        workflow_id=uuid4(),
        workflow_version_id=uuid4(),
        node_id=body.node_id,
        credential_bindings=body.credentials,
        credential_resolver=request.app.state.credential_resolver,
        http_client=http_client,
        trigger_payload=body.trigger,
        services=dict(getattr(request.app.state.engine, "_services", {})),
    )
    t0 = time.monotonic()
    try:
        raw = await node_cls().execute(validated_cfg, ctx)
        result = raw if isinstance(raw, NodeResult) else NodeResult(output=raw)
        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        return {
            "ok": True,
            "node_id": body.node_id,
            "resolved_input": resolved_cfg,
            "output": result.output,
            "branch": result.branch,
            "duration_ms": duration_ms,
            "logs": [
                {"level": entry.level, "message": entry.message, "fields": entry.fields}
                for entry in ctx.logs
            ],
            "error": None,
        }
    except (FlowPilotNodeError, Exception) as exc:
        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        return {
            "ok": False,
            "node_id": body.node_id,
            "resolved_input": resolved_cfg,
            "output": None,
            "duration_ms": duration_ms,
            "logs": [
                {"level": entry.level, "message": entry.message, "fields": entry.fields}
                for entry in ctx.logs
            ],
            "error": str(exc),
        }


# --- 2. Workflow Folders ---


class FolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    name: str = Field(..., min_length=1, max_length=160)
    parent_id: UUID | None = None


@parity_router.get("/folders")
async def list_folders(
    project_id: UUID | None = None,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(FolderModel).where(FolderModel.organization_id == tenant.organization_id)
    if project_id:
        stmt = stmt.where(FolderModel.project_id == project_id)
    rows = (await session.execute(stmt.order_by(FolderModel.name.asc()))).scalars().all()
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id),
            "name": r.name,
            "parent_id": str(r.parent_id) if r.parent_id else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@parity_router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: FolderCreateRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")
    row = FolderModel(
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=body.name.strip(),
        parent_id=body.parent_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "created_at": row.created_at.isoformat(),
    }


@parity_router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: UUID,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await session.get(FolderModel, folder_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found.")
    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 3. Built-In Data Tables CRUD ---


class DataTableCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    columns_schema: list[dict[str, Any]] = Field(default_factory=list)


class DataTableRowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] = Field(default_factory=dict)


@parity_router.get("/tables")
async def list_data_tables(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(DataTableModel)
                .where(DataTableModel.organization_id == tenant.organization_id)
                .order_by(DataTableModel.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "description": r.description,
            "columns_schema": r.columns_schema,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@parity_router.post("/tables", status_code=status.HTTP_201_CREATED)
async def create_data_table(
    body: DataTableCreateRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = DataTableModel(
        organization_id=tenant.organization_id,
        name=body.name.strip(),
        description=body.description,
        columns_schema=body.columns_schema,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "columns_schema": row.columns_schema,
        "created_at": row.created_at.isoformat(),
    }


@parity_router.get("/tables/{table_id}/rows")
async def list_table_rows(
    table_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    tbl = await session.get(DataTableModel, table_id)
    if tbl is None or tbl.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Table not found.")
    rows = (
        (
            await session.execute(
                select(DataTableRowModel)
                .where(
                    DataTableRowModel.organization_id == tenant.organization_id,
                    DataTableRowModel.table_id == table_id,
                )
                .order_by(DataTableRowModel.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    return [
        {"id": str(r.id), "data": r.row_data, "created_at": r.created_at.isoformat()} for r in rows
    ]


@parity_router.post("/tables/{table_id}/rows", status_code=status.HTTP_201_CREATED)
async def insert_table_row(
    table_id: UUID,
    body: DataTableRowRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    tbl = await session.get(DataTableModel, table_id)
    if tbl is None or tbl.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Table not found.")
    row = DataTableRowModel(
        organization_id=tenant.organization_id,
        table_id=tbl.id,
        row_data=body.data,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": str(row.id), "data": row.row_data, "created_at": row.created_at.isoformat()}


@parity_router.delete("/tables/{table_id}/rows/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_table_row(
    table_id: UUID,
    row_id: UUID,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await session.get(DataTableRowModel, row_id)
    if (
        row is None
        or row.organization_id != tenant.organization_id
        or row.table_id != table_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Row not found.")
    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 4. Binary File Handling ---


class BinaryFileUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., min_length=1, max_length=256)
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    content_base64: str = Field(..., min_length=1)


@parity_router.post("/files/upload", status_code=status.HTTP_201_CREATED)
async def upload_binary_file(
    body: BinaryFileUploadRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid base64 payload."
        ) from exc
    file_id = uuid4()
    artifact_store = request.app.state.artifact_store
    ref = await artifact_store.put_json(
        organization_id=tenant.organization_id,
        run_id=file_id,
        payload={"b64": body.content_base64},
    )

    row = BinaryFileModel(
        id=file_id,
        organization_id=tenant.organization_id,
        filename=body.filename,
        mime_type=body.mime_type,
        size_bytes=len(raw),
        storage_key=ref.model_dump_json(),
    )
    session.add(row)
    await session.commit()
    return {
        "id": str(row.id),
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "download_url": f"/api/v1/files/{row.id}/download",
    }


@parity_router.get("/files")
async def list_binary_files(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(BinaryFileModel)
                .where(BinaryFileModel.organization_id == tenant.organization_id)
                .order_by(BinaryFileModel.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "filename": r.filename,
            "mime_type": r.mime_type,
            "size_bytes": r.size_bytes,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@parity_router.get("/files/{file_id}/download")
async def download_binary_file(
    file_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await session.get(BinaryFileModel, file_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found.")
    artifact_store = request.app.state.artifact_store
    ref = ArtifactReference.model_validate_json(row.storage_key)
    envelope = await artifact_store.get_json(ref)
    data = base64.b64decode(str(envelope["b64"]))
    return Response(
        content=data,
        media_type=row.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


# --- 5. Knowledge Base / RAG Pipeline ---


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 60) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + chunk_size)
        chunks.append(cleaned[start:end].strip())
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class KnowledgeDocCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=256)
    content: str = Field(..., min_length=1)
    collection: str = Field(default="default", min_length=1, max_length=128)
    mime_type: str = Field(default="text/markdown", max_length=128)


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    collection: str | None = None


@parity_router.post("/knowledge/documents", status_code=status.HTTP_201_CREATED)
async def ingest_knowledge_document(
    body: KnowledgeDocCreateRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    pieces = _chunk_text(body.content)
    doc = KnowledgeDocumentModel(
        organization_id=tenant.organization_id,
        title=body.title.strip(),
        collection=body.collection.strip(),
        mime_type=body.mime_type,
        chunk_count=len(pieces),
    )
    session.add(doc)
    await session.flush()
    for idx, piece in enumerate(pieces):
        session.add(
            KnowledgeChunkModel(
                organization_id=tenant.organization_id,
                document_id=doc.id,
                collection=doc.collection,
                chunk_index=idx,
                content=piece,
                embedding_json=deterministic_embed(piece),
            )
        )
    await session.commit()
    return {
        "id": str(doc.id),
        "title": doc.title,
        "collection": doc.collection,
        "chunk_count": doc.chunk_count,
    }


@parity_router.get("/knowledge/documents")
async def list_knowledge_documents(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(KnowledgeDocumentModel)
                .where(KnowledgeDocumentModel.organization_id == tenant.organization_id)
                .order_by(KnowledgeDocumentModel.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "title": r.title,
            "collection": r.collection,
            "mime_type": r.mime_type,
            "chunk_count": r.chunk_count,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@parity_router.post("/knowledge/search")
async def search_knowledge_base(
    body: KnowledgeSearchRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    search_fn = request.app.state.knowledge_search_service
    chunks = await search_fn(tenant.organization_id, body.query, body.top_k, body.collection)
    return {"query": body.query, "count": len(chunks), "chunks": chunks}


# --- 6. Insights & Analytics Dashboard ---


@parity_router.get("/insights")
async def get_workspace_insights(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    runs = (
        (
            await session.execute(
                select(WorkflowRunModel).where(
                    WorkflowRunModel.organization_id == tenant.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    total = len(runs)
    succeeded = sum(1 for r in runs if r.status == "SUCCEEDED")
    failed = sum(1 for r in runs if r.status == "FAILED")
    waiting = sum(1 for r in runs if r.status == "WAITING_APPROVAL")

    durations_ms: list[int] = []
    for r in runs:
        if r.started_at and r.finished_at:
            durations_ms.append(max(0, int((r.finished_at - r.started_at).total_seconds() * 1000)))
    durations_ms.sort()
    avg_ms = int(sum(durations_ms) / len(durations_ms)) if durations_ms else 0
    p95_ms = durations_ms[int(len(durations_ms) * 0.95)] if durations_ms else 0

    usage_rows = (
        (
            await session.execute(
                select(UsageRecordModel).where(
                    UsageRecordModel.organization_id == tenant.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    ai_tokens = int(
        sum(
            u.quantity
            for u in usage_rows
            if u.metric in {"ai_tokens_input", "ai_tokens_output"}
        )
    )
    ai_cost_usd = round(
        sum(u.quantity for u in usage_rows if u.metric == "ai_cost_usd"),
        6,
    )
    # Estimate ~4 minutes of manual work saved per succeeded automated workflow run
    estimated_hours_saved = round((succeeded * 4.0) / 60.0, 2)
    return {
        "total_runs": total,
        "succeeded_runs": succeeded,
        "failed_runs": failed,
        "waiting_approval_runs": waiting,
        "success_rate_pct": round((succeeded / total) * 100.0, 1) if total > 0 else 100.0,
        "avg_duration_ms": avg_ms,
        "p95_duration_ms": p95_ms,
        "total_ai_tokens": ai_tokens,
        "total_ai_cost_usd": ai_cost_usd,
        "estimated_hours_saved": estimated_hours_saved,
    }


# --- 7. Hosted ChatHub / Embeddable Conversational Interface ---


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=8000)
    session_id: UUID | None = None


@parity_router.post("/chat/{workflow_id}/messages")
async def send_chat_message(
    workflow_id: UUID,
    body: ChatMessageIn,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or wf_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    if wf_row.active_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Publish the workflow before chatting with it.",
        )
    ver_row = await session.get(WorkflowVersionModel, wf_row.active_version_id)
    assert ver_row is not None

    chat_session = (
        await session.get(ChatSessionModel, body.session_id) if body.session_id else None
    )
    if chat_session is None or chat_session.organization_id != tenant.organization_id:
        chat_session = ChatSessionModel(
            organization_id=tenant.organization_id,
            workflow_id=wf_row.id,
            title=body.message[:80],
        )
        session.add(chat_session)
        await session.flush()

    prev_msgs = (
        (
            await session.execute(
                select(ChatMessageModel)
                .where(ChatMessageModel.session_id == chat_session.id)
                .order_by(ChatMessageModel.created_at.asc())
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in prev_msgs]

    session.add(
        ChatMessageModel(
            organization_id=tenant.organization_id,
            session_id=chat_session.id,
            role="user",
            content=body.message,
        )
    )
    await session.commit()

    version = WorkflowVersion(
        id=ver_row.id,
        workflow_id=ver_row.workflow_id,
        organization_id=ver_row.organization_id,
        version_number=ver_row.version_number,
        definition=WorkflowDefinition.model_validate(ver_row.definition_json),
        content_hash=ver_row.content_hash,
        published_at=ver_row.published_at,
        published_by=ver_row.published_by,
        changelog=ver_row.changelog,
    )
    engine: WorkflowExecutionEngine = request.app.state.engine
    run = await engine.start_run(
        version,
        organization_id=tenant.organization_id,
        trigger_input={
            "session_id": str(chat_session.id),
            "message": body.message,
            "history": history,
        },
        trigger_type="chat",
    )
    output = await engine.payloads.unpack(run.output_ref)

    reply_text = ""
    if isinstance(output, dict):
        for val in reversed(list(output.values())):
            if isinstance(val, dict):
                for key in ("reply", "text", "result", "answer", "message"):
                    if val.get(key):
                        reply_text = str(val[key])
                        break
            if reply_text:
                break
    if not reply_text:
        reply_text = f"Workflow completed ({run.status.value}): {output}"

    session.add(
        ChatMessageModel(
            organization_id=tenant.organization_id,
            session_id=chat_session.id,
            role="assistant",
            content=reply_text,
            run_id=run.id,
        )
    )
    await session.commit()
    return {
        "session_id": str(chat_session.id),
        "run_id": str(run.id),
        "status": run.status.value,
        "reply": reply_text,
        "output": output,
    }


@parity_router.get("/chat/{workflow_id}/sessions/{session_id}")
async def get_chat_session_messages(
    workflow_id: UUID,
    session_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    chat_sess = await session.get(ChatSessionModel, session_id)
    if (
        chat_sess is None
        or chat_sess.organization_id != tenant.organization_id
        or chat_sess.workflow_id != workflow_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    msgs = (
        (
            await session.execute(
                select(ChatMessageModel)
                .where(ChatMessageModel.session_id == session_id)
                .order_by(ChatMessageModel.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "session_id": str(chat_sess.id),
        "workflow_id": str(workflow_id),
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


# --- 8. Interactive AI Assistant (Edit & Debug Live Canvas) ---


class AiAssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=2, max_length=4000)
    graph: WorkflowGraph
    workflow_name: str = "Workflow"


@parity_router.post("/ai/assistant")
async def run_ai_workflow_assistant(
    body: AiAssistantRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    """Conversational AI assistant that inspects, edits, and repairs a live workflow graph."""
    updated = body.graph.model_copy(deep=True)
    msg = body.message.lower()
    actions_taken: list[str] = []

    existing_ids = {n.id for n in updated.nodes}
    tail_id = updated.nodes[-1].id if updated.nodes else None
    xs = [n.ui.position.x if n.ui is not None else 0.0 for n in updated.nodes]
    next_x = (max(xs, default=0.0) + 280.0) if updated.nodes else 80.0

    def _append_node(node_id: str, node_type: str, name: str, config: dict[str, Any]) -> None:
        nonlocal tail_id, next_x
        cid = node_id
        idx = 2
        while cid in existing_ids:
            cid = f"{node_id}_{idx}"
            idx += 1
        existing_ids.add(cid)
        updated.nodes.append(
            NodeDefinition(
                id=cid,
                type=node_type,
                name=name,
                config=config,
                ui=NodeUI(position=Position(x=next_x, y=140)),
            )
        )
        if tail_id:
            updated.edges.append(
                EdgeDefinition(id=f"e_{tail_id}_{cid}", source=tail_id, target=cid)
            )
        tail_id = cid
        next_x += 280

    if "slack" in msg:
        _append_node(
            "notify_slack",
            "slack.post_message",
            "Notify Slack",
            {"channel": "#ops-alerts", "text": "Workflow completed: {{ trigger }}"},
        )
        actions_taken.append("Added Slack notification node connected to the workflow tail.")
    if "approval" in msg or "approve" in msg:
        _append_node(
            "human_gate",
            "core.human_approval",
            "Human Approval Gate",
            {"title": "Approve automated action", "fail_on_reject": False},
        )
        actions_taken.append("Added Human Approval Gate node.")
    if "switch" in msg or "route" in msg:
        _append_node(
            "route_switch",
            "core.switch",
            "Route Switch",
            {"value": "{{ trigger.tier }}", "routes": {"vip": "enterprise", "std": "standard"}},
        )
        actions_taken.append("Added multi-branch Switch router node.")
    if "rag" in msg or "knowledge" in msg:
        _append_node(
            "rag_lookup",
            "core.knowledge_search",
            "Knowledge Search (RAG)",
            {"query": "{{ trigger.question }}", "top_k": 5},
        )
        actions_taken.append("Added Knowledge Base RAG retrieval node.")
    if "table" in msg or "store" in msg or "save" in msg:
        _append_node(
            "save_table",
            "core.data_table",
            "Save to Data Table",
            {
                "table_name": "workflow_records",
                "operation": "insert",
                "data": {"raw": "{{ trigger }}"},
            },
        )
        actions_taken.append("Added Built-in Data Table insert node.")
    if "js" in msg or "javascript" in msg or "code" in msg:
        _append_node(
            "js_transform",
            "core.js_code",
            "JavaScript Transform",
            {"code": "return { processed: true, keys: Object.keys(input) };"},
        )
        actions_taken.append("Added sandboxed JavaScript Code node.")

    if not actions_taken:
        # Run live validator diagnostics and optionally ask configured LLM for guidance
        defn = WorkflowDefinition(
            organization_id=tenant.organization_id,
            project_id=uuid4(),
            name=body.workflow_name,
            graph=updated,
        )
        val = WorkflowValidator(request.app.state.registry).validate(defn)
        llm_provider = getattr(request.app.state, "agent_model_provider", None)
        if llm_provider is not None:
            try:
                comp = await llm_provider.complete(
                    provider="openai",
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are FlowPilot AI Assistant. Help the user improve or debug "
                                "their workflow concisely."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Workflow '{body.workflow_name}' has {len(updated.nodes)} nodes "
                                f"and {len(val.issues)} validation issues. Request: {body.message}"
                            ),
                        },
                    ],
                )
                if comp.content:
                    return {
                        "reply": comp.content,
                        "modified": False,
                        "graph": updated.model_dump(mode="json"),
                        "validation": val.model_dump(mode="json"),
                    }
            except Exception:
                pass
        if val.issues:
            summary = "; ".join(f"[{i.code}] {i.message}" for i in val.issues[:5])
            reply = f"I inspected your workflow and found {len(val.issues)} issue(s): {summary}"
        else:
            reply = (
                "Your workflow graph passes all validation checks! Ask me to add a Slack alert, "
                "Human Approval Gate, Switch router, RAG knowledge search, Data Table step, "
                "or JavaScript Code node and I will update the canvas directly."
            )
        return {
            "reply": reply,
            "modified": False,
            "graph": updated.model_dump(mode="json"),
            "validation": val.model_dump(mode="json"),
        }

    defn = WorkflowDefinition(
        organization_id=tenant.organization_id,
        project_id=uuid4(),
        name=body.workflow_name,
        graph=updated,
    )
    val = WorkflowValidator(request.app.state.registry).validate(defn)
    return {
        "reply": " ".join(actions_taken),
        "modified": True,
        "graph": updated.model_dump(mode="json"),
        "validation": val.model_dump(mode="json"),
    }


# --- 9. OpenAPI-to-Node Dynamic Connector Importer ---


class OpenApiImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(..., min_length=2, max_length=48)
    base_url: str | None = None
    spec: dict[str, Any]
    max_operations: int = Field(default=30, ge=1, le=100)


@parity_router.post("/connectors/openapi/import", status_code=status.HTTP_201_CREATED)
async def import_openapi_connectors(
    body: OpenApiImportRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
) -> dict[str, Any]:
    """Generate and register typed FlowPilot nodes from any OpenAPI 3.x specification."""
    try:
        node_classes = generate_nodes_from_openapi(
            body.spec,
            prefix=body.prefix,
            base_url=body.base_url,
            max_operations=body.max_operations,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    registry: NodeRegistry = request.app.state.registry
    registered_types: list[str] = []
    for cls in node_classes:
        registry.register(cls, replace=True)
        registered_types.append(cls.metadata.type)
    return {
        "prefix": body.prefix,
        "registered_count": len(registered_types),
        "node_types": registered_types,
    }
