"""Browser JWT auth, org switching, OAuth2 credential flows, cursor pagination, DLQ,
Stripe checkout/portal/reconciliation, and marketplace package submission routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot import (
    MarketplaceSubmissionPipeline,
    NodePackageManifest,
    PackageTrustError,
    PackageTrustPolicy,
)
from flowpilot_api.auth import (
    SESSION_COOKIE_NAME,
    TenantContext,
    create_session_token,
    get_db_session,
    get_tenant_context,
    hash_password,
    require_permission,
    verify_password,
)
from flowpilot_api.db.models import (
    CredentialModel,
    MarketplacePackageModel,
    OrganizationMemberModel,
    OrganizationModel,
    ProjectModel,
    SubscriptionModel,
    UsageRecordModel,
    UserModel,
    WorkflowModel,
    WorkspaceModel,
)
from flowpilot_policy import PlanTier, Role
from flowpilot_schema import WorkflowDefinition

platform_router = APIRouter(prefix="/api/v1")


# --- 1. Browser Session Auth & Organization Switching ---


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=200)
    organization_name: str = Field(..., min_length=1, max_length=200)
    organization_slug: str = Field(..., min_length=2, max_length=120, pattern=r"^[a-z0-9-]+$")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=128)
    organization_id: UUID | None = None


class SwitchOrgRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID


@platform_router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register_user(
    body: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    email = body.email.lower().strip()
    existing = (
        await session.execute(select(UserModel).where(UserModel.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered.",
        )

    user = UserModel(
        email=email,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
    )
    org = OrganizationModel(name=body.organization_name, slug=body.organization_slug)
    session.add_all([user, org])
    await session.flush()

    member = OrganizationMemberModel(
        organization_id=org.id,
        user_id=user.id,
        role=Role.OWNER.value,
    )
    workspace = WorkspaceModel(organization_id=org.id, name="Default Workspace")
    session.add_all([member, workspace])
    await session.flush()

    project = ProjectModel(
        organization_id=org.id,
        workspace_id=workspace.id,
        name="Default Project",
    )
    session.add(project)
    await session.commit()

    token = create_session_token(
        secret=request.app.state.settings.session_secret,
        user_id=user.id,
        email=user.email,
        organization_id=org.id,
        role=Role.OWNER,
        ttl_seconds=request.app.state.settings.session_ttl_seconds,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=request.app.state.settings.session_ttl_seconds,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": str(user.id),
        "email": user.email,
        "organization_id": str(org.id),
        "workspace_id": str(workspace.id),
        "project_id": str(project.id),
        "role": Role.OWNER.value,
    }


@platform_router.post("/auth/login")
async def login_user(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    email = body.email.lower().strip()
    user = (
        await session.execute(select(UserModel).where(UserModel.email == email))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    memberships = (
        await session.execute(
            select(OrganizationMemberModel).where(OrganizationMemberModel.user_id == user.id)
        )
    ).scalars().all()
    if not memberships:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has no organization memberships.",
        )

    selected = memberships[0]
    if body.organization_id is not None:
        match = next((m for m in memberships if m.organization_id == body.organization_id), None)
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not a member of the requested organization.",
            )
        selected = match

    role = Role(selected.role) if selected.role in {r.value for r in Role} else Role.VIEWER
    token = create_session_token(
        secret=request.app.state.settings.session_secret,
        user_id=user.id,
        email=user.email,
        organization_id=selected.organization_id,
        role=role,
        ttl_seconds=request.app.state.settings.session_ttl_seconds,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=request.app.state.settings.session_ttl_seconds,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": str(user.id),
        "email": user.email,
        "organization_id": str(selected.organization_id),
        "role": role.value,
        "organizations": [
            {"organization_id": str(m.organization_id), "role": m.role} for m in memberships
        ],
    }


@platform_router.get("/auth/me")
async def get_me(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    orgs: list[dict[str, str]] = []
    if tenant.user_id is not None:
        memberships = (
            await session.execute(
                select(OrganizationMemberModel).where(
                    OrganizationMemberModel.user_id == tenant.user_id
                )
            )
        ).scalars().all()
        orgs = [{"organization_id": str(m.organization_id), "role": m.role} for m in memberships]
    return {
        "actor": tenant.actor,
        "user_id": str(tenant.user_id) if tenant.user_id else None,
        "organization_id": str(tenant.organization_id),
        "role": tenant.role.value if tenant.role else None,
        "scopes": sorted(tenant.scopes),
        "organizations": orgs,
    }


@platform_router.post("/auth/switch-organization")
async def switch_organization(
    body: SwitchOrgRequest,
    request: Request,
    response: Response,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization switching requires a user session token.",
        )
    membership = (
        await session.execute(
            select(OrganizationMemberModel).where(
                OrganizationMemberModel.user_id == tenant.user_id,
                OrganizationMemberModel.organization_id == body.organization_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of target organization.",
        )
    role = Role(membership.role) if membership.role in {r.value for r in Role} else Role.VIEWER
    token = create_session_token(
        secret=request.app.state.settings.session_secret,
        user_id=tenant.user_id,
        email=tenant.actor,
        organization_id=membership.organization_id,
        role=role,
        ttl_seconds=request.app.state.settings.session_ttl_seconds,
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=request.app.state.settings.session_ttl_seconds,
    )
    return {
        "access_token": token,
        "organization_id": str(membership.organization_id),
        "role": role.value,
    }


@platform_router.post("/auth/logout")
async def logout_user(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"logged_out": True}


# --- 2. OAuth2 PKCE Credential Connection Flow ---

OAUTH_PROVIDERS: dict[str, dict[str, str]] = {
    "slack": {
        "authorize_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "credential_type": "slack_bot",
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "credential_type": "google_oauth",
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "credential_type": "github_oauth",
    },
}


class OAuthAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(..., min_length=2)
    redirect_uri: str = Field(..., min_length=8)
    scopes: list[str] = Field(default_factory=list)
    credential_name: str = Field(default="OAuth Connection", max_length=200)


class OAuthCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    code: str
    client_id: str
    client_secret: str


def _sign_oauth_state(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    sig = hmac.new(secret.encode("utf-8"), b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_oauth_state(state: str, secret: str) -> dict[str, Any] | None:
    if "." not in state:
        return None
    b64, sig = state.rsplit(".", 1)
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


@platform_router.post("/oauth/{provider}/authorize")
async def oauth_authorize(
    provider: str,
    body: OAuthAuthorizeRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("credentials:write")),
) -> dict[str, str]:
    cfg = OAUTH_PROVIDERS.get(provider.lower())
    if cfg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unsupported OAuth provider '{provider}'.",
        )
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = _sign_oauth_state(
        {
            "org_id": str(tenant.organization_id),
            "provider": provider.lower(),
            "redirect_uri": body.redirect_uri,
            "scopes": body.scopes,
            "credential_name": body.credential_name,
            "code_verifier": code_verifier,
            "exp": int(time.time()) + 600,
        },
        secret=request.app.state.settings.session_secret,
    )
    scope_str = " ".join(body.scopes)
    auth_url = (
        f"{cfg['authorize_url']}?response_type=code&client_id={body.client_id}"
        f"&redirect_uri={body.redirect_uri}&scope={scope_str}"
        f"&code_challenge={code_challenge}&code_challenge_method=S256&state={state}"
    )
    return {"authorization_url": auth_url, "state": state}


@platform_router.post("/oauth/{provider}/callback", status_code=status.HTTP_201_CREATED)
async def oauth_callback(
    provider: str,
    body: OAuthCallbackRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("credentials:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    cfg = OAUTH_PROVIDERS.get(provider.lower())
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unsupported provider.")

    state_data = _verify_oauth_state(body.state, secret=request.app.state.settings.session_secret)
    if (
        state_data is None
        or state_data.get("provider") != provider.lower()
        or state_data.get("org_id") != str(tenant.organization_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired, or cross-tenant OAuth state token.",
        )

    transport = getattr(request.app.state, "oauth_http_transport", None)
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        resp = await client.post(
            cfg["token_url"],
            data={
                "grant_type": "authorization_code",
                "code": body.code,
                "client_id": body.client_id,
                "client_secret": body.client_secret,
                "redirect_uri": state_data["redirect_uri"],
                "code_verifier": state_data["code_verifier"],
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"OAuth token exchange failed (HTTP {resp.status_code}).",
            )
        token_payload = resp.json()

    access_token = str(token_payload.get("access_token", ""))
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OAuth provider response missing access_token.",
        )

    secrets_dict = {"token": access_token}
    if token_payload.get("refresh_token"):
        secrets_dict["refresh_token"] = str(token_payload["refresh_token"])

    cred_id = uuid4()
    envelope = request.app.state.cipher.encrypt_secrets(
        secrets_dict,
        organization_id=tenant.organization_id,
        credential_id=cred_id,
    )
    row = CredentialModel(
        id=cred_id,
        organization_id=tenant.organization_id,
        name=str(state_data.get("credential_name", f"{provider} OAuth")),
        credential_type=cfg["credential_type"],
        encrypted_envelope=envelope.model_dump(mode="json"),
        metadata_json={"provider": provider.lower(), "oauth": True},
        scopes=list(state_data.get("scopes", [])),
    )
    session.add(row)
    await session.commit()
    return {
        "credential_id": str(cred_id),
        "name": row.name,
        "credential_type": row.credential_type,
        "provider": provider.lower(),
        "key_id": envelope.key_id,
    }


# --- 3. Cursor Pagination, DLQ, Stripe Checkout/Portal/Reconciliation & Marketplace Submissions ---


@platform_router.get("/workflows/paginated")
async def list_workflows_cursor(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    tenant: TenantContext = Depends(require_permission("workflows:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Cursor-paginated workflow listing ordered stably by (created_at, id)."""
    stmt = select(WorkflowModel).where(WorkflowModel.organization_id == tenant.organization_id)
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor).decode("utf-8"))
            cursor_ts = datetime.fromisoformat(decoded["created_at"])
            stmt = stmt.where(WorkflowModel.created_at < cursor_ts)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid pagination cursor.",
            ) from exc

    stmt = stmt.order_by(WorkflowModel.created_at.desc(), WorkflowModel.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        raw_cur = json.dumps(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        ).encode("utf-8")
        next_cursor = base64.urlsafe_b64encode(raw_cur).decode("ascii")

    return {
        "items": [
            {
                "id": str(r.id),
                "name": r.name,
                "revision": r.revision,
                "is_active": r.is_active,
                "active_version_id": str(r.active_version_id) if r.active_version_id else None,
                "definition": WorkflowDefinition.model_validate(r.draft_definition).model_dump(
                    mode="json"
                ),
            }
            for r in page
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@platform_router.get("/dlq")
async def list_dead_letter_queue(
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:read")),
) -> list[dict[str, Any]]:
    entries = await request.app.state.dlq.list_for_org(tenant.organization_id)
    return [e.model_dump(mode="json") for e in entries]


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: PlanTier
    success_url: str = "http://localhost:3000/billing/success"
    cancel_url: str = "http://localhost:3000/billing/cancel"


class PortalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_url: str = "http://localhost:3000/billing"


@platform_router.post("/billing/checkout")
async def create_billing_checkout(
    body: CheckoutRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("billing:manage")),
) -> dict[str, Any]:
    res: dict[str, Any] = await request.app.state.billing_client.create_checkout_session(
        organization_id=tenant.organization_id,
        plan=body.plan,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
    )
    return res


@platform_router.post("/billing/portal")
async def create_billing_portal(
    body: PortalRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("billing:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    sub = (
        await session.execute(
            select(SubscriptionModel).where(
                SubscriptionModel.organization_id == tenant.organization_id
            )
        )
    ).scalar_one_or_none()
    cust_id = (
        sub.stripe_customer_id
        if sub is not None and sub.stripe_customer_id
        else f"cus_local_{tenant.organization_id.hex[:12]}"
    )
    res: dict[str, Any] = await request.app.state.billing_client.create_portal_session(
        stripe_customer_id=cust_id,
        return_url=body.return_url,
    )
    return res


@platform_router.post("/billing/reconcile")
async def reconcile_billing_period(
    request: Request,
    period_key: str = Query(default="current"),
    tenant: TenantContext = Depends(require_permission("billing:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Aggregate tenant usage_records and report idempotent metered usage to Stripe."""
    rows = (
        await session.execute(
            select(UsageRecordModel.metric, func.sum(UsageRecordModel.quantity))
            .where(UsageRecordModel.organization_id == tenant.organization_id)
            .group_by(UsageRecordModel.metric)
        )
    ).all()
    reported: list[dict[str, Any]] = []
    cust_id = f"cus_{tenant.organization_id.hex[:12]}"
    for metric, qty in rows:
        count = round(float(qty or 0.0))
        if count <= 0:
            continue
        res = await request.app.state.billing_client.report_meter_event(
            event_name=f"flowpilot.{metric}",
            stripe_customer_id=cust_id,
            value=count,
            idempotency_key=f"reconcile:{tenant.organization_id}:{period_key}:{metric}",
        )
        reported.append(res)
    return {
        "organization_id": str(tenant.organization_id),
        "period_key": period_key,
        "reported_events": reported,
    }


class MarketplaceSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: NodePackageManifest
    source_files: dict[str, str] = Field(..., min_length=1)
    publisher: str = Field(..., min_length=1, max_length=128)


@platform_router.post("/marketplace/submissions", status_code=status.HTTP_201_CREATED)
async def submit_marketplace_package(
    body: MarketplaceSubmissionRequest,
    request: Request,
    tenant: TenantContext = Depends(require_permission("workflows:publish")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Scan third-party package source AST, Ed25519-sign, and publish to marketplace catalog."""
    pipeline: MarketplaceSubmissionPipeline = request.app.state.marketplace_pipeline
    try:
        approved_manifest, report = pipeline.review_and_approve(
            manifest=body.manifest,
            source_files=body.source_files,
        )
    except PackageTrustError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    pkg = MarketplacePackageModel(
        package_name=approved_manifest.package_name,
        package_version=approved_manifest.package_version,
        publisher=body.publisher,
        first_party=False,
        manifest=approved_manifest.model_dump(mode="json"),
    )
    session.add(pkg)
    await session.commit()
    await session.refresh(pkg)
    return {
        "package_id": str(pkg.id),
        "package_name": pkg.package_name,
        "package_version": pkg.package_version,
        "sha256_digest": approved_manifest.sha256_digest,
        "signature": approved_manifest.signature,
        "scan_passed": report.passed,
    }


__all__ = ["PackageTrustPolicy", "platform_router"]
