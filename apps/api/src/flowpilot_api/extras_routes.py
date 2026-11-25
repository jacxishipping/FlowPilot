"""Agents API, signed webhook ingestion, Stripe billing webhooks, and Prometheus metrics."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_agents import (
    AgentBudget,
    AgentDefinition,
    AgentRuntime,
    EscalationBehavior,
    MemoryPolicy,
)
from flowpilot_api.auth import TenantContext, get_db_session, get_tenant_context
from flowpilot_api.db.models import (
    AgentModel,
    AgentVersionModel,
    AuditLogModel,
    OrganizationModel,
    ProjectModel,
    SubscriptionModel,
    UsageRecordModel,
    WorkflowModel,
    WorkflowVersionModel,
)
from flowpilot_api.webhooks import verify_webhook_signature
from flowpilot_engine import WorkflowExecutionEngine, WorkflowExecutionError
from flowpilot_nodes_core import ModelCompletion, ModelProvider
from flowpilot_policy import evaluate_quota, get_plan_limits
from flowpilot_schema import WorkflowDefinition, WorkflowVersion

extras_router = APIRouter()


class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    name: str = Field(..., min_length=1, max_length=200)
    objective: str = Field(..., min_length=1)
    system_instructions: str = ""
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    allowed_tools: list[str] = Field(default_factory=list)
    credential_scopes: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    escalation_behavior: EscalationBehavior = EscalationBehavior.FAIL


class AgentOut(BaseModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    name: str
    definition: AgentDefinition
    created_at: datetime


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_input: str | None = None
    approved_tools: list[str] = Field(default_factory=list)


class _EchoFallbackProvider:
    """Deterministic fallback model provider when no external LLM provider is attached."""

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion:
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return ModelCompletion(
            content=f"[{provider}/{model}] Completed bounded objective: {last_user}",
            input_tokens=max(10, len(last_user) // 4),
            output_tokens=24,
            cost_usd=0.0005,
        )


# --- Agents Endpoints (/api/v1/agents) ---


@extras_router.get("/api/v1/agents", response_model=list[AgentOut])
async def list_agents(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[AgentOut]:
    stmt = select(AgentModel).where(AgentModel.organization_id == tenant.organization_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        AgentOut(
            id=r.id,
            organization_id=r.organization_id,
            project_id=r.project_id,
            name=r.name,
            definition=AgentDefinition.model_validate(r.config),
            created_at=r.created_at,
        )
        for r in rows
    ]


@extras_router.post(
    "/api/v1/agents",
    response_model=AgentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent(
    body: AgentCreateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> AgentOut:
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    agent_def = AgentDefinition(
        organization_id=tenant.organization_id,
        name=body.name,
        objective=body.objective,
        system_instructions=body.system_instructions,
        provider=body.provider,
        model=body.model,
        allowed_tools=body.allowed_tools,
        credential_scopes=body.credential_scopes,
        approval_required_tools=body.approval_required_tools,
        memory_policy=body.memory_policy,
        budget=body.budget,
        escalation_behavior=body.escalation_behavior,
    )
    row = AgentModel(
        id=agent_def.id,
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=agent_def.name,
        config=agent_def.model_dump(mode="json"),
    )
    ver = AgentVersionModel(
        organization_id=tenant.organization_id,
        agent_id=agent_def.id,
        version_number=1,
        config=agent_def.model_dump(mode="json"),
    )
    session.add_all([row, ver])
    await session.commit()
    await session.refresh(row)

    return AgentOut(
        id=row.id,
        organization_id=row.organization_id,
        project_id=row.project_id,
        name=row.name,
        definition=agent_def,
        created_at=row.created_at,
    )


@extras_router.post("/api/v1/agents/{agent_id}/runs", status_code=status.HTTP_201_CREATED)
async def run_agent(
    agent_id: UUID,
    body: AgentRunRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = await session.get(AgentModel, agent_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")

    agent_def = AgentDefinition.model_validate(row.config)
    provider: ModelProvider = request.app.state.agent_model_provider or _EchoFallbackProvider()
    runtime = AgentRuntime(
        model_provider=provider,
        tool_gateway=request.app.state.tool_gateway,
        memory_store=request.app.state.memory_store,
    )
    result = await runtime.run(
        agent_def,
        organization_id=tenant.organization_id,
        task_input=body.task_input,
        approved_tools=set(body.approved_tools),
    )
    request.app.state.metrics.inc("flowpilot_agent_runs_total", status=result.status)
    return result.model_dump(mode="json")


# --- Signed Webhook Ingestion (/api/v1/webhooks/{workflow_id}) ---


@extras_router.post("/api/v1/webhooks/{workflow_id}", status_code=status.HTTP_202_ACCEPTED)
async def ingest_signed_webhook(
    workflow_id: UUID,
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-FlowPilot-Signature"),
    x_timestamp: str | None = Header(default=None, alias="X-FlowPilot-Timestamp"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Verify HMAC-SHA256 webhook signature and trigger the active pinned workflow version."""
    raw_body = await request.body()
    secret = request.app.state.settings.webhook_signing_secret
    if not x_signature or not verify_webhook_signature(
        secret=secret,
        raw_body=raw_body,
        signature_header=x_signature,
        timestamp_header=x_timestamp,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired webhook HMAC signature.",
        )

    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or not wf_row.is_active or wf_row.active_version_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Active published workflow not found.",
        )

    ver_row = await session.get(WorkflowVersionModel, wf_row.active_version_id)
    if ver_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pinned workflow version not found.",
        )

    try:
        parsed_body: Any = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except ValueError:
        parsed_body = {"raw": raw_body.decode("utf-8", errors="replace")}

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
            organization_id=wf_row.organization_id,
            trigger_input={
                "body": parsed_body,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "query": dict(request.query_params),
            },
            trigger_type="webhook",
            idempotency_key=idempotency_key,
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    request.app.state.metrics.inc("flowpilot_webhook_runs_total", status=run.status.value)
    return {"run_id": str(run.id), "status": run.status.value}


# --- Billing, Plan Quotas, and Idempotent Stripe Webhooks ---


@extras_router.get("/api/v1/billing/summary")
async def get_billing_summary(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    org = await session.get(OrganizationModel, tenant.organization_id)
    plan_name = org.plan if org is not None else "free"
    limits = get_plan_limits(plan_name)

    rows = (
        await session.execute(
            select(UsageRecordModel.metric, func.sum(UsageRecordModel.quantity))
            .where(UsageRecordModel.organization_id == tenant.organization_id)
            .group_by(UsageRecordModel.metric)
        )
    ).all()
    usage = {str(m): float(q or 0.0) for m, q in rows}
    ai_tokens = usage.get("ai_tokens_input", 0.0) + usage.get("ai_tokens_output", 0.0)

    quotas = {
        "workflow_run": evaluate_quota(
            plan_name=plan_name,
            metric="workflow_run",
            current_usage=usage.get("workflow_run", 0.0),
            requested_increment=0.0,
        ).model_dump(mode="json"),
        "node_execution": evaluate_quota(
            plan_name=plan_name,
            metric="node_execution",
            current_usage=usage.get("node_execution", 0.0),
            requested_increment=0.0,
        ).model_dump(mode="json"),
        "ai_tokens": evaluate_quota(
            plan_name=plan_name,
            metric="ai_tokens",
            current_usage=ai_tokens,
            requested_increment=0.0,
        ).model_dump(mode="json"),
    }
    return {
        "organization_id": str(tenant.organization_id),
        "plan": limits.model_dump(mode="json"),
        "usage": usage,
        "quotas": quotas,
    }


@extras_router.post("/api/v1/billing/webhooks/stripe")
async def handle_stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Idempotent Stripe webhook receiver verifying HMAC signature and updating subscriptions."""
    raw_body = await request.body()
    secret = request.app.state.settings.stripe_webhook_secret
    if not stripe_signature or not verify_webhook_signature(
        secret=secret,
        raw_body=raw_body,
        signature_header=stripe_signature,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Stripe webhook signature.",
        )

    event = json.loads(raw_body.decode("utf-8"))
    event_id = str(event.get("id", ""))
    event_type = str(event.get("type", ""))
    if not event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing event id.")

    # Idempotency check: skip already-processed Stripe event IDs
    existing = (
        await session.execute(
            select(AuditLogModel).where(
                AuditLogModel.action == "billing.stripe_event",
                AuditLogModel.resource_id == event_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"received": True, "idempotent_replay": True, "event_id": event_id}

    obj = event.get("data", {}).get("object", {})
    metadata = obj.get("metadata", {})
    org_id_raw = metadata.get("organization_id")
    if not org_id_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Stripe event missing metadata.organization_id.",
        )
    org_id = UUID(str(org_id_raw))
    org = await session.get(OrganizationModel, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    new_plan = str(metadata.get("plan", org.plan)).lower()
    sub_status = str(obj.get("status", "active"))
    if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
        org.plan = new_plan
    elif event_type == "customer.subscription.deleted":
        org.plan = "free"
        sub_status = "canceled"

    sub_row = (
        await session.execute(
            select(SubscriptionModel).where(SubscriptionModel.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if sub_row is None:
        session.add(
            SubscriptionModel(
                organization_id=org_id,
                stripe_customer_id=obj.get("customer"),
                stripe_subscription_id=obj.get("id"),
                plan=org.plan,
                status=sub_status,
            )
        )
    else:
        sub_row.plan = org.plan
        sub_row.status = sub_status
        sub_row.stripe_customer_id = obj.get("customer", sub_row.stripe_customer_id)
        sub_row.stripe_subscription_id = obj.get("id", sub_row.stripe_subscription_id)
        sub_row.updated_at = datetime.now(UTC)

    session.add(
        AuditLogModel(
            organization_id=org_id,
            actor="stripe:webhook",
            action="billing.stripe_event",
            resource_type="stripe_event",
            resource_id=event_id,
            details={"event_type": event_type, "plan": org.plan, "status": sub_status},
        )
    )
    await session.commit()
    request.app.state.metrics.inc("flowpilot_billing_events_total", event_type=event_type)
    return {
        "received": True,
        "idempotent_replay": False,
        "event_id": event_id,
        "plan": org.plan,
    }


# --- Prometheus Metrics Endpoint (/metrics) ---


@extras_router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(request: Request) -> str:
    return str(request.app.state.metrics.render_prometheus())
