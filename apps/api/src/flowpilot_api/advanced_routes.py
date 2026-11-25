"""Track 1 & 2 API routes: Time-Travel Replay, Workflow Evals, Inbound MCP Server,
Version Diff & Rollback, Approvals Inbox, Signed Action Links, Templates, and Preview."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_api.auth import TenantContext, get_db_session, get_tenant_context, require_permission
from flowpilot_api.db.models import (
    ApprovalModel,
    ProjectModel,
    WorkflowModel,
    WorkflowRunModel,
    WorkflowVersionModel,
)
from flowpilot_api.templates import CURATED_TEMPLATES
from flowpilot_engine import (
    EvalCase,
    WorkflowEvalRunner,
    WorkflowExecutionEngine,
    WorkflowExecutionError,
)
from flowpilot_expressions import ExpressionError, resolve_template
from flowpilot_schema import WorkflowDefinition, WorkflowVersion

advanced_router = APIRouter(prefix="/api/v1")


def _compute_version_diff(
    old_def: WorkflowDefinition,
    new_def: WorkflowDefinition,
) -> dict[str, Any]:
    old_nodes = {n.id: n for n in old_def.graph.nodes}
    new_nodes = {n.id: n for n in new_def.graph.nodes}
    old_edges = {e.id: e for e in old_def.graph.edges}
    new_edges = {e.id: e for e in new_def.graph.edges}

    added_nodes = sorted(set(new_nodes) - set(old_nodes))
    removed_nodes = sorted(set(old_nodes) - set(new_nodes))
    modified_nodes: list[dict[str, Any]] = []
    for nid in sorted(set(old_nodes) & set(new_nodes)):
        a = old_nodes[nid].model_dump(mode="json", exclude={"ui"})
        b = new_nodes[nid].model_dump(mode="json", exclude={"ui"})
        if a != b:
            changed = sorted(k for k in (set(a) | set(b)) if a.get(k) != b.get(k))
            modified_nodes.append(
                {"node_id": nid, "changed_fields": changed, "before": a, "after": b}
            )
    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "added_edges": sorted(set(new_edges) - set(old_edges)),
        "removed_edges": sorted(set(old_edges) - set(new_edges)),
    }


def _sign_action_token(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_action_token(token: str, secret: str) -> dict[str, Any] | None:
    if "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


# --- 1. Time-Travel Replay ("Fork & Replay from Node") ---


class ReplayFromNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_node_id: str = Field(..., min_length=1, max_length=64)
    config_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pinned_outputs: dict[str, Any] = Field(default_factory=dict)


@advanced_router.post("/runs/{run_id}/replay", status_code=status.HTTP_201_CREATED)
async def replay_run_from_node(
    run_id: UUID,
    body: ReplayFromNodeRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    run_row = await session.get(WorkflowRunModel, run_id)
    if run_row is None or run_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source run not found.")

    ver_row = await session.get(WorkflowVersionModel, run_row.workflow_version_id)
    if ver_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pinned workflow version not found.",
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
        forked = await engine.replay_from_node(
            run_id,
            version,
            organization_id=tenant.organization_id,
            start_node_id=body.start_node_id,
            config_overrides=body.config_overrides or None,
            pinned_outputs=body.pinned_outputs or None,
        )
    except WorkflowExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return forked.model_dump(mode="json")


# --- 2. Workflow & Agent Evaluation Suite ---


class RunEvalSuiteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version_id: UUID | None = None
    cases: list[EvalCase] = Field(..., min_length=1, max_length=100)


@advanced_router.post("/workflows/{workflow_id}/evals/run")
async def run_workflow_evals(
    workflow_id: UUID,
    body: RunEvalSuiteRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or wf_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    ver_id = body.workflow_version_id or wf_row.active_version_id
    if ver_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Publish a workflow version before running evaluations.",
        )
    ver_row = await session.get(WorkflowVersionModel, ver_id)
    if ver_row is None or ver_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found.")

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
    runner = WorkflowEvalRunner(request.app.state.engine)
    suite_result = await runner.run_suite(version, body.cases)
    return suite_result.model_dump(mode="json")


# --- 3. Inbound MCP JSON-RPC 2.0 Server (/api/v1/mcp) ---


class McpRpcRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    jsonrpc: str = "2.0"
    id: int | str | None = 1
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@advanced_router.post("/mcp")
async def inbound_mcp_server(
    body: McpRpcRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Expose active published FlowPilot workflows as governed MCP tools (JSON-RPC 2.0)."""
    if body.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "flowpilot-mcp-server", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        }

    if body.method == "tools/list":
        stmt = select(WorkflowModel).where(
            WorkflowModel.organization_id == tenant.organization_id,
            WorkflowModel.is_active.is_(True),
            WorkflowModel.active_version_id.is_not(None),
        )
        rows = (await session.execute(stmt)).scalars().all()
        tools = [
            {
                "name": f"workflow_{r.id.hex[:12]}",
                "description": f"{r.name}: {r.description}".strip(": "),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                "annotations": {"workflow_id": str(r.id)},
            }
            for r in rows
        ]
        return {"jsonrpc": "2.0", "id": body.id, "result": {"tools": tools}}

    if body.method == "tools/call":
        tool_name = str(body.params.get("name", ""))
        arguments = body.params.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {"input": arguments}

        stmt = select(WorkflowModel).where(
            WorkflowModel.organization_id == tenant.organization_id,
            WorkflowModel.is_active.is_(True),
            WorkflowModel.active_version_id.is_not(None),
        )
        rows = (await session.execute(stmt)).scalars().all()
        target = next(
            (r for r in rows if f"workflow_{r.id.hex[:12]}" == tool_name or str(r.id) == tool_name),
            None,
        )
        if target is None or target.active_version_id is None:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "error": {"code": -32602, "message": f"Unknown MCP workflow tool '{tool_name}'."},
            }

        ver_row = await session.get(WorkflowVersionModel, target.active_version_id)
        assert ver_row is not None
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
            trigger_input=arguments,
            trigger_type="mcp",
        )
        output = await engine.payloads.unpack(run.output_ref)
        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "run_id": str(run.id),
                                "status": run.status.value,
                                "output": output,
                            }
                        ),
                    }
                ],
                "isError": run.status.value == "FAILED",
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": body.id,
        "error": {"code": -32601, "message": f"Method '{body.method}' not found."},
    }


# --- 4. Visual Version Diff & One-Click Rollback ---


@advanced_router.get("/workflows/{workflow_id}/diff")
async def diff_workflow_versions(
    workflow_id: UUID,
    from_version: int = Query(..., ge=1),
    to_version: int = Query(..., ge=1),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    v_from = (
        await session.execute(
            select(WorkflowVersionModel).where(
                WorkflowVersionModel.workflow_id == workflow_id,
                WorkflowVersionModel.organization_id == tenant.organization_id,
                WorkflowVersionModel.version_number == from_version,
            )
        )
    ).scalar_one_or_none()
    v_to = (
        await session.execute(
            select(WorkflowVersionModel).where(
                WorkflowVersionModel.workflow_id == workflow_id,
                WorkflowVersionModel.organization_id == tenant.organization_id,
                WorkflowVersionModel.version_number == to_version,
            )
        )
    ).scalar_one_or_none()
    if v_from is None or v_to is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or both workflow versions not found.",
        )
    diff = _compute_version_diff(
        WorkflowDefinition.model_validate(v_from.definition_json),
        WorkflowDefinition.model_validate(v_to.definition_json),
    )
    return {
        "workflow_id": str(workflow_id),
        "from_version": from_version,
        "to_version": to_version,
        **diff,
    }


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_version_number: int = Field(..., ge=1)


@advanced_router.post("/workflows/{workflow_id}/rollback", status_code=status.HTTP_201_CREATED)
async def rollback_workflow(
    workflow_id: UUID,
    body: RollbackRequest,
    tenant: TenantContext = Depends(require_permission("workflows:publish")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or wf_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    target_ver = (
        await session.execute(
            select(WorkflowVersionModel).where(
                WorkflowVersionModel.workflow_id == workflow_id,
                WorkflowVersionModel.organization_id == tenant.organization_id,
                WorkflowVersionModel.version_number == body.target_version_number,
            )
        )
    ).scalar_one_or_none()
    if target_ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version v{body.target_version_number} not found.",
        )

    max_ver = (
        await session.execute(
            select(func.max(WorkflowVersionModel.version_number)).where(
                WorkflowVersionModel.workflow_id == workflow_id
            )
        )
    ).scalar_one_or_none()
    next_ver_num = (max_ver or 0) + 1

    restored_def = WorkflowDefinition.model_validate(target_ver.definition_json).model_copy(
        update={"revision": wf_row.revision + 1}
    )
    new_ver = WorkflowVersion.from_definition(
        restored_def,
        version_number=next_ver_num,
        changelog=f"Rollback to v{body.target_version_number}",
    )
    session.add(
        WorkflowVersionModel(
            id=new_ver.id,
            organization_id=tenant.organization_id,
            workflow_id=workflow_id,
            version_number=new_ver.version_number,
            definition_json=new_ver.definition.model_dump(mode="json"),
            content_hash=new_ver.content_hash,
            changelog=new_ver.changelog,
            published_at=new_ver.published_at,
        )
    )
    wf_row.revision = restored_def.revision
    wf_row.draft_definition = restored_def.model_dump(mode="json")
    wf_row.active_version_id = new_ver.id
    wf_row.is_active = True
    await session.commit()
    return {
        "workflow_id": str(workflow_id),
        "rolled_back_to": body.target_version_number,
        "new_version_number": new_ver.version_number,
        "revision": wf_row.revision,
        "definition": restored_def.model_dump(mode="json"),
    }


# --- 5. Standalone Approvals Inbox & Signed One-Click Action Links ---


@advanced_router.get("/approvals")
async def list_approvals_inbox(
    decision: str | None = Query(default=None),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(ApprovalModel).where(ApprovalModel.organization_id == tenant.organization_id)
    if decision:
        stmt = stmt.where(ApprovalModel.decision == decision)
    stmt = stmt.order_by(ApprovalModel.requested_at.desc()).limit(100)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "run_id": str(r.run_id),
            "node_id": r.node_id,
            "title": r.title,
            "description": r.description,
            "risk_level": r.risk_level,
            "decision": r.decision,
            "decided_by": r.decided_by,
            "comment": r.comment,
            "payload": r.payload,
            "requested_at": r.requested_at.isoformat(),
        }
        for r in rows
    ]


@advanced_router.post("/approvals/{approval_id}/action-links")
async def generate_approval_action_links(
    approval_id: UUID,
    request: Request,
    ttl_seconds: int = Query(default=3600, ge=60, le=86_400),
    tenant: TenantContext = Depends(require_permission("approvals:decide")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Generate HMAC-signed one-click approve/reject URLs + Slack Block Kit interactive card."""
    row = await session.get(ApprovalModel, approval_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")

    secret = request.app.state.settings.session_secret
    exp = int(time.time()) + ttl_seconds
    approve_tok = _sign_action_token(
        {
            "approval_id": str(row.id),
            "org_id": str(row.organization_id),
            "approved": True,
            "actor": tenant.actor,
            "exp": exp,
        },
        secret,
    )
    reject_tok = _sign_action_token(
        {
            "approval_id": str(row.id),
            "org_id": str(row.organization_id),
            "approved": False,
            "actor": tenant.actor,
            "exp": exp,
        },
        secret,
    )
    base = str(request.base_url).rstrip("/")
    approve_url = f"{base}/api/v1/approvals/action?token={approve_tok}"
    reject_url = f"{base}/api/v1/approvals/action?token={reject_tok}"

    slack_blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Approval Required: {row.title}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Risk:* `{row.risk_level}`\n"
                    f"{row.description or 'Review workflow action.'}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "url": approve_url,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "url": reject_url,
                },
            ],
        },
    ]
    return {
        "approval_id": str(row.id),
        "approve_url": approve_url,
        "reject_url": reject_url,
        "approve_token": approve_tok,
        "reject_token": reject_tok,
        "slack_blocks": slack_blocks,
    }


@advanced_router.get("/approvals/action")
async def execute_signed_approval_action(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """One-click signed Approve/Reject link handler for Slack & email approvals."""
    claims = _verify_action_token(token, request.app.state.settings.session_secret)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired approval action link.",
        )
    approval_id = UUID(str(claims["approval_id"]))
    org_id = UUID(str(claims["org_id"]))
    row = await session.get(ApprovalModel, approval_id)
    if row is None or row.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")

    engine: WorkflowExecutionEngine = request.app.state.engine
    resumed = await engine.submit_approval_decision(
        row.run_id,
        approval_id,
        organization_id=org_id,
        approved=bool(claims["approved"]),
        decided_by=f"action_link:{claims['actor']}",
        comment="Decided via signed one-click action link",
    )
    return {
        "approval_id": str(approval_id),
        "approved": bool(claims["approved"]),
        "run_status": resumed.status.value,
    }


# --- 6. Curated Templates Gallery & Live Expression Preview ---


@advanced_router.get("/templates")
async def list_templates() -> list[dict[str, Any]]:
    return [tpl.to_summary() for tpl in CURATED_TEMPLATES.values()]


class CloneTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    name: str | None = None


@advanced_router.post("/templates/{slug}/clone", status_code=status.HTTP_201_CREATED)
async def clone_template(
    slug: str,
    body: CloneTemplateRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    tpl = CURATED_TEMPLATES.get(slug)
    if tpl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    definition = tpl.instantiate(organization_id=tenant.organization_id, project_id=proj.id)
    if body.name:
        definition = definition.model_copy(update={"name": body.name})
    wf_id = uuid4()
    definition = definition.model_copy(update={"id": wf_id})
    row = WorkflowModel(
        id=wf_id,
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=definition.name,
        description=definition.description,
        revision=1,
        draft_definition=definition.model_dump(mode="json"),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "revision": row.revision,
        "definition": definition.model_dump(mode="json"),
    }


class ExpressionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str
    pinned_nodes: dict[str, Any] = Field(default_factory=dict)
    trigger: dict[str, Any] = Field(default_factory=dict)


@advanced_router.post("/expressions/preview")
async def preview_expression(body: ExpressionPreviewRequest) -> dict[str, Any]:
    """Evaluate a FlowPilot expression against pinned node outputs for Studio live preview."""
    ctx = {
        "nodes": {nid: {"output": out} for nid, out in body.pinned_nodes.items()},
        "trigger": body.trigger,
        "inputs": body.trigger,
    }
    try:
        result = resolve_template(body.template, ctx)
        return {"ok": True, "value": result, "error": None}
    except ExpressionError as exc:
        return {"ok": False, "value": None, "error": str(exc)}
