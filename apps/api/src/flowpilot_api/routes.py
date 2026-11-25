"""FastAPI /api/v1 router for workflows, durable runs, SSE events, credentials, and AI drafts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_api.ai_generator import generate_workflow_draft
from flowpilot_api.auth import (
    TenantContext,
    generate_api_key,
    get_db_session,
    get_tenant_context,
    hash_password,
)
from flowpilot_api.db.models import (
    ApiKeyModel,
    ApprovalModel,
    AuditLogModel,
    CredentialModel,
    IntegrationPackageModel,
    MarketplacePackageModel,
    OrganizationMemberModel,
    OrganizationModel,
    ProjectModel,
    UsageRecordModel,
    UserModel,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowVersionModel,
    WorkspaceModel,
)
from flowpilot_api.schemas import (
    ApprovalDecisionRequest,
    BootstrapRequest,
    BootstrapResponse,
    CredentialCreateRequest,
    CredentialOut,
    MarketplacePackageOut,
    OrganizationCreate,
    OrganizationOut,
    ProjectCreate,
    ProjectOut,
    PublishWorkflowRequest,
    RunCreateRequest,
    WorkflowCreate,
    WorkflowGenerationRequest,
    WorkflowGenerationResponse,
    WorkflowOut,
    WorkflowPatch,
    WorkflowVersionOut,
    WorkspaceCreate,
    WorkspaceOut,
)
from flowpilot_engine import WorkflowExecutionEngine, WorkflowExecutionError, format_sse
from flowpilot_schema import (
    RunStatus,
    WorkflowDefinition,
    WorkflowVersion,
    export_workflow_json_schema,
)
from flowpilot_validator import ValidationResult, WorkflowValidator

router = APIRouter(prefix="/api/v1")


async def _audit(
    session: AsyncSession,
    *,
    organization_id: UUID,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLogModel(
            organization_id=organization_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )


def _workflow_to_out(row: WorkflowModel) -> WorkflowOut:
    return WorkflowOut(
        id=row.id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        name=row.name,
        description=row.description,
        revision=row.revision,
        is_active=row.is_active,
        active_version_id=row.active_version_id,
        definition=WorkflowDefinition.model_validate(row.draft_definition),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- Public Catalog & Bootstrap Endpoints ---


@router.get("/nodes/catalog")
async def get_node_catalog(request: Request) -> dict[str, object]:
    """Return all registered nodes with config & output JSON Schemas for editor & AI."""
    return {"nodes": request.app.state.registry.export_catalog()}


@router.get("/schemas/workflow")
async def get_workflow_schema() -> dict[str, object]:
    """Return canonical WorkflowDefinition JSON Schema."""
    return export_workflow_json_schema()


@router.post(
    "/auth/bootstrap",
    response_model=BootstrapResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bootstrap_tenant(
    body: BootstrapRequest,
    session: AsyncSession = Depends(get_db_session),
) -> BootstrapResponse:
    """Create initial user, organization, workspace, project, and scoped API key."""
    existing_org = (
        await session.execute(
            select(OrganizationModel).where(OrganizationModel.slug == body.organization_slug)
        )
    ).scalar_one_or_none()
    if existing_org is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug '{body.organization_slug}' already exists.",
        )

    user = UserModel(
        email=body.email.lower().strip(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
    )
    org = OrganizationModel(name=body.organization_name, slug=body.organization_slug)
    session.add_all([user, org])
    await session.flush()

    member = OrganizationMemberModel(organization_id=org.id, user_id=user.id, role="owner")
    workspace = WorkspaceModel(organization_id=org.id, name=body.workspace_name)
    session.add_all([member, workspace])
    await session.flush()

    project = ProjectModel(
        organization_id=org.id,
        workspace_id=workspace.id,
        name=body.project_name,
    )
    raw_key, key_prefix, key_hash = generate_api_key()
    api_key = ApiKeyModel(
        organization_id=org.id,
        name="Default Admin Key",
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=["*"] ,
    )
    session.add_all([project, api_key])
    await _audit(
        session,
        organization_id=org.id,
        actor=user.email,
        action="tenant.bootstrap",
        resource_type="organization",
        resource_id=str(org.id),
    )
    await session.commit()

    return BootstrapResponse(
        user_id=user.id,
        organization_id=org.id,
        workspace_id=workspace.id,
        project_id=project.id,
        api_key=raw_key,
        api_key_prefix=key_prefix,
    )


# --- Organizations, Workspaces & Projects ---


@router.get("/organizations", response_model=list[OrganizationOut])
async def list_organizations(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[OrganizationOut]:
    org = await session.get(OrganizationModel, tenant.organization_id)
    if org is None:
        return []
    return [
        OrganizationOut(
            id=org.id,
            name=org.name,
            slug=org.slug,
            plan=org.plan,
            created_at=org.created_at,
        )
    ]


@router.post("/organizations", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationOut:
    org = OrganizationModel(name=body.name, slug=body.slug)
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return OrganizationOut(
        id=org.id, name=org.name, slug=org.slug, plan=org.plan, created_at=org.created_at
    )


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkspaceOut]:
    stmt = select(WorkspaceModel).where(WorkspaceModel.organization_id == tenant.organization_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        WorkspaceOut(
            id=r.id,
            organization_id=r.organization_id,
            name=r.name,
            description=r.description,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/workspaces", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceOut:
    ws = WorkspaceModel(
        organization_id=tenant.organization_id,
        name=body.name,
        description=body.description,
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)
    return WorkspaceOut(
        id=ws.id,
        organization_id=ws.organization_id,
        name=ws.name,
        description=ws.description,
        created_at=ws.created_at,
    )


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProjectOut]:
    stmt = select(ProjectModel).where(ProjectModel.organization_id == tenant.organization_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        ProjectOut(
            id=r.id,
            organization_id=r.organization_id,
            workspace_id=r.workspace_id,
            name=r.name,
            description=r.description,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> ProjectOut:
    ws = await session.get(WorkspaceModel, body.workspace_id)
    if ws is None or ws.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found.")
    proj = ProjectModel(
        organization_id=tenant.organization_id,
        workspace_id=ws.id,
        name=body.name,
        description=body.description,
    )
    session.add(proj)
    await session.commit()
    await session.refresh(proj)
    return ProjectOut(
        id=proj.id,
        organization_id=proj.organization_id,
        workspace_id=proj.workspace_id,
        name=proj.name,
        description=proj.description,
        created_at=proj.created_at,
    )


# --- Workflows, Optimistic Revision Saves, Validation & Publishing ---


@router.get("/workflows", response_model=list[WorkflowOut])
async def list_workflows(
    project_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkflowOut]:
    stmt = select(WorkflowModel).where(WorkflowModel.organization_id == tenant.organization_id)
    if project_id is not None:
        stmt = stmt.where(WorkflowModel.project_id == project_id)
    stmt = stmt.order_by(WorkflowModel.updated_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [_workflow_to_out(r) for r in rows]


@router.post("/workflows", response_model=WorkflowOut, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowOut:
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    wf_id = uuid4()
    definition = WorkflowDefinition(
        id=wf_id,
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=body.name,
        description=body.description,
        revision=1,
        graph=body.graph,
        settings=body.settings,
        output_mapping=body.output_mapping,
        tags=body.tags,
    )
    row = WorkflowModel(
        id=wf_id,
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=body.name,
        description=body.description,
        revision=1,
        draft_definition=definition.model_dump(mode="json"),
    )
    session.add(row)
    await _audit(
        session,
        organization_id=tenant.organization_id,
        actor=tenant.actor,
        action="workflow.create",
        resource_type="workflow",
        resource_id=str(wf_id),
    )
    await session.commit()
    await session.refresh(row)
    return _workflow_to_out(row)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowOut:
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    return _workflow_to_out(row)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
async def patch_workflow(
    workflow_id: UUID,
    body: WorkflowPatch,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowOut:
    """Revision-aware optimistic save preventing concurrent editors from overwriting silently."""
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    if row.revision != body.expected_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Revision conflict: expected revision {body.expected_revision}, "
                f"current revision is {row.revision}. Reload latest draft before saving."
            ),
        )

    current_def = WorkflowDefinition.model_validate(row.draft_definition)
    next_revision = row.revision + 1
    updated_def = current_def.model_copy(
        update={
            "name": body.name if body.name is not None else current_def.name,
            "description": (
                body.description if body.description is not None else current_def.description
            ),
            "graph": body.graph if body.graph is not None else current_def.graph,
            "settings": body.settings if body.settings is not None else current_def.settings,
            "output_mapping": (
                body.output_mapping
                if body.output_mapping is not None
                else current_def.output_mapping
            ),
            "tags": body.tags if body.tags is not None else current_def.tags,
            "revision": next_revision,
        }
    )

    row.name = updated_def.name
    row.description = updated_def.description
    row.revision = next_revision
    row.draft_definition = updated_def.model_dump(mode="json")
    await session.commit()
    await session.refresh(row)
    return _workflow_to_out(row)


@router.post("/workflows/{workflow_id}/validate", response_model=ValidationResult)
async def validate_workflow(
    workflow_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> ValidationResult:
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    definition = WorkflowDefinition.model_validate(row.draft_definition)
    validator = WorkflowValidator(request.app.state.registry)
    return validator.validate(definition)


@router.post(
    "/workflows/{workflow_id}/publish",
    response_model=WorkflowVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def publish_workflow(
    workflow_id: UUID,
    body: PublishWorkflowRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowVersionOut:
    """Validate draft and create an immutable pinned WorkflowVersion."""
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    definition = WorkflowDefinition.model_validate(row.draft_definition)
    validator = WorkflowValidator(request.app.state.registry)
    validation = validator.validate(definition)
    if not validation.is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Workflow failed pre-publish validation.",
                "errors": [e.model_dump(mode="json") for e in validation.errors],
            },
        )

    max_ver = (
        await session.execute(
            select(func.max(WorkflowVersionModel.version_number)).where(
                WorkflowVersionModel.workflow_id == workflow_id
            )
        )
    ).scalar_one_or_none()
    next_version_number = (max_ver or 0) + 1

    version = WorkflowVersion.from_definition(
        definition,
        version_number=next_version_number,
        changelog=body.changelog,
    )
    ver_row = WorkflowVersionModel(
        id=version.id,
        organization_id=tenant.organization_id,
        workflow_id=workflow_id,
        version_number=version.version_number,
        definition_json=version.definition.model_dump(mode="json"),
        content_hash=version.content_hash,
        changelog=version.changelog,
        published_at=version.published_at,
    )
    session.add(ver_row)
    row.active_version_id = version.id
    if body.activate:
        row.is_active = True
    await _audit(
        session,
        organization_id=tenant.organization_id,
        actor=tenant.actor,
        action="workflow.publish",
        resource_type="workflow_version",
        resource_id=str(version.id),
        details={"version_number": version.version_number, "content_hash": version.content_hash},
    )
    await session.commit()

    return WorkflowVersionOut(
        id=version.id,
        workflow_id=workflow_id,
        version_number=version.version_number,
        content_hash=version.content_hash,
        changelog=version.changelog,
        published_at=version.published_at,
    )


@router.get("/workflows/{workflow_id}/versions", response_model=list[WorkflowVersionOut])
async def list_workflow_versions(
    workflow_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkflowVersionOut]:
    stmt = (
        select(WorkflowVersionModel)
        .where(
            WorkflowVersionModel.workflow_id == workflow_id,
            WorkflowVersionModel.organization_id == tenant.organization_id,
        )
        .order_by(WorkflowVersionModel.version_number.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        WorkflowVersionOut(
            id=r.id,
            workflow_id=r.workflow_id,
            version_number=r.version_number,
            content_hash=r.content_hash,
            changelog=r.changelog,
            published_at=r.published_at,
        )
        for r in rows
    ]


@router.post("/workflows/{workflow_id}/activate", response_model=WorkflowOut)
async def activate_workflow(
    workflow_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowOut:
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    if row.active_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Publish a version before activating the workflow.",
        )
    row.is_active = True
    await session.commit()
    await session.refresh(row)
    return _workflow_to_out(row)


@router.post("/workflows/{workflow_id}/deactivate", response_model=WorkflowOut)
async def deactivate_workflow(
    workflow_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowOut:
    row = await session.get(WorkflowModel, workflow_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    row.is_active = False
    await session.commit()
    await session.refresh(row)
    return _workflow_to_out(row)


# --- Workflow Execution, SSE Event Stream, Approvals, Cancel & Retry ---


@router.post("/workflows/{workflow_id}/runs", status_code=status.HTTP_201_CREATED)
async def start_workflow_run(
    workflow_id: UUID,
    body: RunCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Start a durable run pinned to an immutable published WorkflowVersion."""
    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or wf_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    version_id = body.workflow_version_id or wf_row.active_version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workflow has no published version. Call POST /workflows/{id}/publish first.",
        )

    ver_row = await session.get(WorkflowVersionModel, version_id)
    if ver_row is None or ver_row.organization_id != tenant.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workflow version not found."
        )

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
    try:
        run = await engine.start_run(
            version,
            organization_id=tenant.organization_id,
            trigger_input=body.trigger_input,
            trigger_type=body.trigger_type,
            idempotency_key=idempotency_key,
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return run.model_dump(mode="json")


@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, object]]:
    stmt = (
        select(WorkflowRunModel)
        .where(
            WorkflowRunModel.workflow_id == workflow_id,
            WorkflowRunModel.organization_id == tenant.organization_id,
        )
        .order_by(WorkflowRunModel.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [r.run_state_json for r in rows]


@router.get("/runs/{run_id}")
async def get_run(
    run_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    row = await session.get(WorkflowRunModel, run_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return row.run_state_json


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    follow: bool = Query(default=False),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Server-Sent Events (SSE) stream of ordered ExecutionEvents for a run."""
    row = await session.get(WorkflowRunModel, run_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")

    start_seq = after_sequence
    if last_event_id and last_event_id.isdigit():
        start_seq = max(start_seq, int(last_event_id))

    event_sink = request.app.state.event_sink
    store = request.app.state.execution_store

    async def _event_generator() -> AsyncIterator[str]:
        cursor = start_seq
        while True:
            events = await event_sink.list_events(
                run_id,
                organization_id=tenant.organization_id,
                after_sequence=cursor,
            )
            for evt in events:
                cursor = evt.sequence
                yield format_sse(evt)

            if not follow:
                break

            latest = await store.get_run(run_id)
            if latest is None or latest.status.is_terminal:
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
) -> dict[str, object]:
    engine: WorkflowExecutionEngine = request.app.state.engine
    try:
        run = await engine.cancel_run(run_id, organization_id=tenant.organization_id)
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return run.model_dump(mode="json")


@router.post("/runs/{run_id}/retry", status_code=status.HTTP_201_CREATED)
async def retry_run(
    run_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Start a fresh run against the same pinned version and input payload."""
    row = await session.get(WorkflowRunModel, run_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")

    ver_row = await session.get(WorkflowVersionModel, row.workflow_version_id)
    if ver_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Pinned workflow version not found."
        )
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
    old_run = await engine.store.get_run(run_id)
    trigger_input = await engine.payloads.unpack(old_run.input_ref) if old_run else {}
    new_run = await engine.start_run(
        version,
        organization_id=tenant.organization_id,
        trigger_input=trigger_input if isinstance(trigger_input, dict) else {},
        trigger_type="retry",
    )
    return new_run.model_dump(mode="json")


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Approve or reject a pending human-in-the-loop gate and resume the paused run."""
    approval_row = await session.get(ApprovalModel, approval_id)
    if approval_row is None or approval_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")

    engine: WorkflowExecutionEngine = request.app.state.engine
    try:
        resumed = await engine.submit_approval_decision(
            approval_row.run_id,
            approval_id,
            organization_id=tenant.organization_id,
            approved=body.approved,
            decided_by=tenant.actor,
            comment=body.comment,
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return resumed.model_dump(mode="json")


# --- Envelope-Encrypted Credentials ---


@router.get("/credentials", response_model=list[CredentialOut])
async def list_credentials(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[CredentialOut]:
    stmt = select(CredentialModel).where(
        CredentialModel.organization_id == tenant.organization_id
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        CredentialOut(
            id=r.id,
            organization_id=r.organization_id,
            name=r.name,
            credential_type=r.credential_type,
            key_id=str(r.encrypted_envelope.get("key_id", "")),
            metadata=r.metadata_json,
            scopes=list(r.scopes),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/credentials", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
async def create_credential(
    body: CredentialCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> CredentialOut:
    """Encrypt secrets with tenant-bound AES-256-GCM envelope encryption and store ciphertext."""
    cred_id = uuid4()
    envelope = request.app.state.cipher.encrypt_secrets(
        body.secrets,
        organization_id=tenant.organization_id,
        credential_id=cred_id,
    )
    row = CredentialModel(
        id=cred_id,
        organization_id=tenant.organization_id,
        name=body.name,
        credential_type=body.credential_type,
        encrypted_envelope=envelope.model_dump(mode="json"),
        metadata_json=body.metadata,
        scopes=body.scopes,
    )
    session.add(row)
    await _audit(
        session,
        organization_id=tenant.organization_id,
        actor=tenant.actor,
        action="credential.create",
        resource_type="credential",
        resource_id=str(cred_id),
        details={"credential_type": body.credential_type, "key_id": envelope.key_id},
    )
    await session.commit()
    await session.refresh(row)

    return CredentialOut(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        credential_type=row.credential_type,
        key_id=envelope.key_id,
        metadata=row.metadata_json,
        scopes=list(row.scopes),
        created_at=row.created_at,
    )


# --- AI Natural-Language Workflow Generation ---


@router.post("/ai/workflow-generation", response_model=WorkflowGenerationResponse)
async def generate_workflow(
    body: WorkflowGenerationRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> WorkflowGenerationResponse:
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    draft, missing_config, notes = generate_workflow_draft(
        prompt=body.prompt,
        organization_id=tenant.organization_id,
        project_id=body.project_id,
        registry=request.app.state.registry,
        name=body.name,
    )
    validator = WorkflowValidator(request.app.state.registry)
    validation = validator.validate(draft)
    return WorkflowGenerationResponse(
        draft=draft,
        validation=validation,
        missing_configuration=missing_config,
        notes=notes,
    )


# --- Marketplace & Usage Ledger ---


@router.get("/marketplace/packages", response_model=list[MarketplacePackageOut])
async def list_marketplace_packages(
    session: AsyncSession = Depends(get_db_session),
) -> list[MarketplacePackageOut]:
    rows = (await session.execute(select(MarketplacePackageModel))).scalars().all()
    return [
        MarketplacePackageOut(
            id=r.id,
            package_name=r.package_name,
            package_version=r.package_version,
            publisher=r.publisher,
            first_party=r.first_party,
            manifest=r.manifest,
            published_at=r.published_at,
        )
        for r in rows
    ]


@router.post(
    "/marketplace/packages/{package_id}/install",
    status_code=status.HTTP_201_CREATED,
)
async def install_marketplace_package(
    package_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    pkg = await session.get(MarketplacePackageModel, package_id)
    if pkg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Package not found.")
    installed = IntegrationPackageModel(
        organization_id=tenant.organization_id,
        package_name=pkg.package_name,
        package_version=pkg.package_version,
        manifest=pkg.manifest,
    )
    session.add(installed)
    await session.commit()
    return {
        "installed_id": str(installed.id),
        "package_name": pkg.package_name,
        "package_version": pkg.package_version,
    }


@router.get("/usage")
async def get_usage_summary(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    stmt = (
        select(UsageRecordModel.metric, func.sum(UsageRecordModel.quantity))
        .where(UsageRecordModel.organization_id == tenant.organization_id)
        .group_by(UsageRecordModel.metric)
    )
    rows = (await session.execute(stmt)).all()
    totals = {str(metric): float(total or 0.0) for metric, total in rows}
    run_count = (
        await session.execute(
            select(func.count(WorkflowRunModel.id)).where(
                WorkflowRunModel.organization_id == tenant.organization_id,
                WorkflowRunModel.status == RunStatus.SUCCEEDED.value,
            )
        )
    ).scalar_one()
    return {
        "organization_id": str(tenant.organization_id),
        "metrics": totals,
        "succeeded_runs": int(run_count),
    }
