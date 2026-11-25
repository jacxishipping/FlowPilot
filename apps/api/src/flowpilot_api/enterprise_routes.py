"""Track 3 Enterprise API routes: Environment Promotion (dev->staging->prod),
SSO (OIDC & SAML), SCIM 2.0 Provisioning, and SIEM Audit Log Export."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot import ControlledHttpClient
from flowpilot_api.auth import (
    SESSION_COOKIE_NAME,
    TenantContext,
    create_session_token,
    get_db_session,
    hash_password,
    require_permission,
    verify_session_token,
)
from flowpilot_api.db.models import (
    AuditLogModel,
    EnvironmentModel,
    OrganizationMemberModel,
    OrganizationModel,
    ProjectModel,
    UserModel,
    WorkflowModel,
    WorkflowVersionModel,
)
from flowpilot_policy import Role
from flowpilot_schema import WorkflowDefinition, WorkflowVersion

enterprise_router = APIRouter(prefix="/api/v1")


# --- 1. Environments & Promotion Pipeline (dev -> staging -> prod) ---


class EnvironmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    name: str = Field(..., pattern=r"^(dev|staging|prod|[a-z0-9_-]{2,32})$")
    variables: dict[str, Any] = Field(default_factory=dict)
    credential_map: dict[str, UUID] = Field(
        default_factory=dict,
        description="Maps source credential UUID (str) -> target environment credential UUID.",
    )


class PromoteWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version_number: int = Field(..., ge=1)
    target_environment_id: UUID
    changelog: str = ""


@enterprise_router.get("/environments")
async def list_environments(
    project_id: UUID | None = None,
    tenant: TenantContext = Depends(require_permission("workflows:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(EnvironmentModel).where(
        EnvironmentModel.organization_id == tenant.organization_id
    )
    if project_id is not None:
        stmt = stmt.where(EnvironmentModel.project_id == project_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id),
            "name": r.name,
            "variables": r.variables,
        }
        for r in rows
    ]


@enterprise_router.post("/environments", status_code=status.HTTP_201_CREATED)
async def create_environment(
    body: EnvironmentCreateRequest,
    tenant: TenantContext = Depends(require_permission("workflows:publish")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    proj = await session.get(ProjectModel, body.project_id)
    if proj is None or proj.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found.")

    env_vars = dict(body.variables)
    if body.credential_map:
        env_vars["_credential_map"] = {k: str(v) for k, v in body.credential_map.items()}

    row = EnvironmentModel(
        organization_id=tenant.organization_id,
        project_id=proj.id,
        name=body.name,
        variables=env_vars,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "variables": row.variables,
    }


@enterprise_router.post("/workflows/{workflow_id}/promote", status_code=status.HTTP_201_CREATED)
async def promote_workflow_version(
    workflow_id: UUID,
    body: PromoteWorkflowRequest,
    tenant: TenantContext = Depends(require_permission("workflows:publish")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Promote an immutable WorkflowVersion to target environment, remapping credentials."""
    wf_row = await session.get(WorkflowModel, workflow_id)
    if wf_row is None or wf_row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")

    target_env = await session.get(EnvironmentModel, body.target_environment_id)
    if target_env is None or target_env.organization_id != tenant.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target environment not found.",
        )

    src_ver = (
        await session.execute(
            select(WorkflowVersionModel).where(
                WorkflowVersionModel.workflow_id == workflow_id,
                WorkflowVersionModel.organization_id == tenant.organization_id,
                WorkflowVersionModel.version_number == body.source_version_number,
            )
        )
    ).scalar_one_or_none()
    if src_ver is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Source version v{body.source_version_number} not found.",
        )

    cred_map = target_env.variables.get("_credential_map", {})
    promoted_def = WorkflowDefinition.model_validate(src_ver.definition_json).model_copy(deep=True)
    remapped_count = 0
    for node in promoted_def.graph.nodes:
        for slot, old_cred_id in list(node.credentials.items()):
            mapped = cred_map.get(str(old_cred_id))
            if mapped:
                node.credentials[slot] = UUID(str(mapped))
                remapped_count += 1

    max_ver = (
        await session.execute(
            select(func.max(WorkflowVersionModel.version_number)).where(
                WorkflowVersionModel.workflow_id == workflow_id
            )
        )
    ).scalar_one_or_none()
    next_num = (max_ver or 0) + 1
    promoted_ver = WorkflowVersion.from_definition(
        promoted_def,
        version_number=next_num,
        changelog=body.changelog
        or f"Promoted v{body.source_version_number} to environment '{target_env.name}'",
    )
    session.add(
        WorkflowVersionModel(
            id=promoted_ver.id,
            organization_id=tenant.organization_id,
            workflow_id=workflow_id,
            version_number=promoted_ver.version_number,
            definition_json=promoted_ver.definition.model_dump(mode="json"),
            content_hash=promoted_ver.content_hash,
            changelog=promoted_ver.changelog,
            published_at=promoted_ver.published_at,
        )
    )
    session.add(
        AuditLogModel(
            organization_id=tenant.organization_id,
            actor=tenant.actor,
            action="workflow.promote",
            resource_type="workflow_version",
            resource_id=str(promoted_ver.id),
            details={
                "source_version": body.source_version_number,
                "target_environment": target_env.name,
                "remapped_credentials": remapped_count,
            },
        )
    )
    wf_row.active_version_id = promoted_ver.id
    await session.commit()
    return {
        "promoted_version_id": str(promoted_ver.id),
        "version_number": promoted_ver.version_number,
        "target_environment": target_env.name,
        "remapped_credentials": remapped_count,
    }


# --- 2. Enterprise SSO (OIDC & SAML) with JIT Provisioning ---


def _map_groups_to_role(groups: list[str]) -> Role:
    lowered = {g.lower() for g in groups}
    if lowered & {"owners", "flowpilot-owners"}:
        return Role.OWNER
    if lowered & {"admins", "flowpilot-admins"}:
        return Role.ADMIN
    if lowered & {"editors", "developers", "flowpilot-editors"}:
        return Role.EDITOR
    if lowered & {"operators", "sre"}:
        return Role.OPERATOR
    return Role.VIEWER


async def _jit_provision_sso_user(
    session: AsyncSession,
    *,
    organization_id: UUID,
    email: str,
    full_name: str,
    role: Role,
    provider: str,
) -> UserModel:
    org = await session.get(OrganizationModel, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    clean_email = email.lower().strip()
    user = (
        await session.execute(select(UserModel).where(UserModel.email == clean_email))
    ).scalar_one_or_none()
    if user is None:
        user = UserModel(
            email=clean_email,
            full_name=full_name,
            password_hash=hash_password(f"sso-jit-{clean_email}-{organization_id}"),
        )
        session.add(user)
        await session.flush()

    member = (
        await session.execute(
            select(OrganizationMemberModel).where(
                OrganizationMemberModel.organization_id == organization_id,
                OrganizationMemberModel.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        session.add(
            OrganizationMemberModel(
                organization_id=organization_id,
                user_id=user.id,
                role=role.value,
            )
        )
    else:
        member.role = role.value

    session.add(
        AuditLogModel(
            organization_id=organization_id,
            actor=clean_email,
            action=f"sso.{provider}.login",
            resource_type="user",
            resource_id=str(user.id),
            details={"role": role.value},
        )
    )
    await session.commit()
    return user


class OidcCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: str
    organization_id: UUID


class SamlAcsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    assertion_b64: str
    signature: str


@enterprise_router.post("/sso/oidc/callback")
async def sso_oidc_callback(
    body: OidcCallbackRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Verify signed OIDC ID token, JIT provision user + role, and issue session JWT."""
    claims = verify_session_token(
        body.id_token,
        secret=request.app.state.settings.session_secret,
    )
    if claims is None or "email" not in claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid OIDC ID token signature or claims.",
        )

    groups = claims.get("groups", [])
    role = _map_groups_to_role(groups if isinstance(groups, list) else [])
    user = await _jit_provision_sso_user(
        session,
        organization_id=body.organization_id,
        email=str(claims["email"]),
        full_name=str(claims.get("name", "")),
        role=role,
        provider="oidc",
    )
    token = create_session_token(
        secret=request.app.state.settings.session_secret,
        user_id=user.id,
        email=user.email,
        organization_id=body.organization_id,
        role=role,
    )
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax")
    return {
        "access_token": token,
        "user_id": str(user.id),
        "email": user.email,
        "role": role.value,
        "provider": "oidc",
    }


@enterprise_router.post("/sso/saml/acs")
async def sso_saml_acs(
    body: SamlAcsRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Verify signed SAML assertion envelope, JIT provision user + role, and issue session JWT."""
    expected_sig = hmac.new(
        request.app.state.settings.session_secret.encode("utf-8"),
        body.assertion_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, body.signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid SAML assertion signature.",
        )
    assertion = json.loads(base64.urlsafe_b64decode(body.assertion_b64).decode("utf-8"))
    groups = assertion.get("groups", [])
    role = _map_groups_to_role(groups if isinstance(groups, list) else [])
    user = await _jit_provision_sso_user(
        session,
        organization_id=body.organization_id,
        email=str(assertion["email"]),
        full_name=str(assertion.get("name", "")),
        role=role,
        provider="saml",
    )
    token = create_session_token(
        secret=request.app.state.settings.session_secret,
        user_id=user.id,
        email=user.email,
        organization_id=body.organization_id,
        role=role,
    )
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, samesite="lax")
    return {
        "access_token": token,
        "user_id": str(user.id),
        "email": user.email,
        "role": role.value,
        "provider": "saml",
    }


# --- 3. SCIM 2.0 User Provisioning & Deprovisioning ---


class ScimUserCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    userName: str
    displayName: str = ""
    active: bool = True
    role: Role = Role.EDITOR


@enterprise_router.get("/scim/v2/Users")
async def scim_list_users(
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    stmt = (
        select(UserModel, OrganizationMemberModel.role)
        .join(OrganizationMemberModel, OrganizationMemberModel.user_id == UserModel.id)
        .where(OrganizationMemberModel.organization_id == tenant.organization_id)
    )
    rows = (await session.execute(stmt)).all()
    resources = [
        {
            "id": str(user.id),
            "userName": user.email,
            "displayName": user.full_name,
            "active": user.is_active,
            "role": role,
        }
        for user, role in rows
    ]
    return {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        "totalResults": len(resources),
        "Resources": resources,
    }


@enterprise_router.post("/scim/v2/Users", status_code=status.HTTP_201_CREATED)
async def scim_create_user(
    body: ScimUserCreate,
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user = await _jit_provision_sso_user(
        session,
        organization_id=tenant.organization_id,
        email=body.userName,
        full_name=body.displayName,
        role=body.role,
        provider="scim",
    )
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": str(user.id),
        "userName": user.email,
        "displayName": user.full_name,
        "active": user.is_active,
    }


@enterprise_router.delete("/scim/v2/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def scim_delete_user(
    user_id: UUID,
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    member = (
        await session.execute(
            select(OrganizationMemberModel).where(
                OrganizationMemberModel.organization_id == tenant.organization_id,
                OrganizationMemberModel.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if member is not None:
        await session.delete(member)
    user = await session.get(UserModel, user_id)
    if user is not None:
        user.is_active = False
    session.add(
        AuditLogModel(
            organization_id=tenant.organization_id,
            actor=tenant.actor,
            action="scim.user.deprovision",
            resource_type="user",
            resource_id=str(user_id),
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 4. SIEM Audit Log Export (NDJSON & CEF) ---


def _format_audit_cef(row: AuditLogModel) -> str:
    return (
        f"CEF:0|FlowPilot|Platform|0.1.0|{row.action}|{row.resource_type}|3|"
        f"suid={row.actor} cs1={row.organization_id} cs2={row.resource_id} "
        f"rt={int(row.created_at.timestamp() * 1000)}"
    )


@enterprise_router.get("/audit-logs/export", response_class=PlainTextResponse)
async def export_audit_logs(
    format: str = Query(default="ndjson", pattern=r"^(ndjson|cef)$"),
    limit: int = Query(default=500, ge=1, le=5000),
    tenant: TenantContext = Depends(require_permission("org:read")),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    stmt = (
        select(AuditLogModel)
        .where(AuditLogModel.organization_id == tenant.organization_id)
        .order_by(AuditLogModel.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if format == "cef":
        return "\n".join(_format_audit_cef(r) for r in rows) + ("\n" if rows else "")
    lines = [
        json.dumps(
            {
                "id": str(r.id),
                "organization_id": str(r.organization_id),
                "actor": r.actor,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "details": r.details,
                "created_at": r.created_at.isoformat(),
            },
            sort_keys=True,
        )
        for r in rows
    ]
    return "\n".join(lines) + ("\n" if lines else "")


class SiemPushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: str = Field(..., min_length=8, max_length=2048)
    format: str = Field(default="ndjson", pattern=r"^(ndjson|cef)$")
    limit: int = Field(default=200, ge=1, le=2000)


@enterprise_router.post("/audit-logs/siem-push")
async def push_audit_logs_to_siem(
    body: SiemPushRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    payload_text = await export_audit_logs(
        format=body.format,
        limit=body.limit,
        tenant=tenant,
        session=session,
    )
    client = ControlledHttpClient(
        allow_private_networks=request.app.state.settings.allow_private_networks,
        transport=getattr(request.app.state, "siem_http_transport", None),
    )
    content_type = "application/x-ndjson" if body.format == "ndjson" else "text/plain"
    resp = await client.request(
        "POST",
        body.endpoint_url,
        headers={"Content-Type": content_type},
        body=payload_text,
    )
    return {
        "pushed": resp.status_code < 400,
        "status_code": resp.status_code,
        "bytes": len(payload_text.encode("utf-8")),
    }
