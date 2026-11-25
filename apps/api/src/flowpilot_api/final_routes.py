"""Final enterprise & parity routes: Collaborative Edit Locks, PDF/Binary RAG Upload,
SSE Streaming Chat, Embeddable Chat Widget, 2FA/TOTP, Custom RBAC Roles, and LDAP SSO."""

from __future__ import annotations

import base64
import json
import re
import zlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from flowpilot_agents import deterministic_embed
from flowpilot_api.auth import (
    SESSION_COOKIE_NAME,
    TenantContext,
    create_session_token,
    get_db_session,
    get_tenant_context,
    require_permission,
)
from flowpilot_api.db.models import (
    AuditLogModel,
    CustomRoleModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    UserMfaModel,
    WorkflowLockModel,
    WorkflowModel,
)
from flowpilot_api.enterprise_routes import _jit_provision_sso_user, _map_groups_to_role
from flowpilot_api.parity_routes import ChatMessageIn, _chunk_text, send_chat_message
from flowpilot_credentials import EncryptedEnvelope
from flowpilot_policy import (
    build_otpauth_uri,
    generate_totp_secret,
    verify_totp_code,
)

final_router = APIRouter(prefix="/api/v1")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --- 1. Collaborative Workflow Edit Locks ---


class AcquireLockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=120, ge=15, le=3600)


@final_router.get("/workflows/{workflow_id}/lock")
async def get_workflow_lock(
    workflow_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    wf = await session.get(WorkflowModel, workflow_id)
    if wf is None or wf.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    lock = await session.get(WorkflowLockModel, workflow_id)
    now = _utc_now()
    if lock is None or _as_utc(lock.expires_at) <= now:
        return {"locked": False, "locked_by": None, "expires_at": None, "is_owner": False}
    return {
        "locked": True,
        "locked_by": lock.locked_by,
        "expires_at": _as_utc(lock.expires_at).isoformat(),
        "is_owner": lock.locked_by == tenant.actor,
    }


@final_router.post("/workflows/{workflow_id}/lock")
async def acquire_workflow_lock(
    workflow_id: UUID,
    body: AcquireLockRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    wf = await session.get(WorkflowModel, workflow_id)
    if wf is None or wf.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found.")
    now = _utc_now()
    expires_at = now + timedelta(seconds=body.ttl_seconds)
    lock = await session.get(WorkflowLockModel, workflow_id)
    if lock is not None and _as_utc(lock.expires_at) > now and lock.locked_by != tenant.actor:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Workflow is currently locked for editing by '{lock.locked_by}'.",
        )
    if lock is None:
        lock = WorkflowLockModel(
            workflow_id=workflow_id,
            organization_id=tenant.organization_id,
            locked_by=tenant.actor,
            expires_at=expires_at,
            updated_at=now,
        )
        session.add(lock)
    else:
        lock.locked_by = tenant.actor
        lock.expires_at = expires_at
        lock.updated_at = now
    await session.commit()
    return {
        "locked": True,
        "locked_by": lock.locked_by,
        "expires_at": _as_utc(lock.expires_at).isoformat(),
        "is_owner": True,
    }


@final_router.delete("/workflows/{workflow_id}/lock", status_code=status.HTTP_204_NO_CONTENT)
async def release_workflow_lock(
    workflow_id: UUID,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    lock = await session.get(WorkflowLockModel, workflow_id)
    if lock is not None and lock.organization_id == tenant.organization_id:
        if lock.locked_by != tenant.actor:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the lock holder can release this edit lock.",
            )
        await session.delete(lock)
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 2. PDF / CSV / Markdown / TXT RAG File Upload & Text Extraction ---


def extract_text_from_bytes(raw: bytes, filename: str, mime_type: str) -> str:
    """Extract readable text from PDF, CSV, Markdown, or TXT bytes cleanly in pure Python."""
    lower_name = filename.lower()
    if lower_name.endswith(".pdf") or mime_type == "application/pdf" or raw.startswith(b"%PDF"):
        parts: list[str] = []
        # Decompress FlateDecode streams if present
        for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw, flags=re.DOTALL):
            stream_data = match.group(1)
            try:
                decoded = zlib.decompress(stream_data)
            except zlib.error:
                decoded = stream_data
            for lit in re.findall(rb"\(([^()\\]{2,})\)", decoded):
                text = lit.decode("latin-1", errors="ignore").strip()
                if text:
                    parts.append(text)
        if not parts:
            # Fallback: extract literal parenthesized PDF text strings directly
            for lit in re.findall(rb"\(([^()\\]{2,})\)", raw):
                text = lit.decode("latin-1", errors="ignore").strip()
                if text:
                    parts.append(text)
        extracted = " ".join(parts).strip()
        if extracted:
            return extracted
    return raw.decode("utf-8", errors="replace").strip()


class KnowledgeFileUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(..., min_length=1, max_length=256)
    mime_type: str = Field(default="text/plain", max_length=128)
    content_base64: str = Field(..., min_length=1)
    collection: str = Field(default="default", min_length=1, max_length=128)


@final_router.post("/knowledge/upload", status_code=status.HTTP_201_CREATED)
async def upload_knowledge_file(
    body: KnowledgeFileUploadRequest,
    tenant: TenantContext = Depends(require_permission("workflows:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Upload PDF/CSV/Markdown/TXT, extract text, chunk, and embed into Knowledge Base."""
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid base64 file content."
        ) from exc

    extracted_text = extract_text_from_bytes(raw, body.filename, body.mime_type)
    pieces = _chunk_text(extracted_text)
    if not pieces:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No extractable text found in uploaded file.",
        )

    doc = KnowledgeDocumentModel(
        organization_id=tenant.organization_id,
        title=body.filename,
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
        "extracted_chars": len(extracted_text),
        "chunk_count": doc.chunk_count,
    }


# --- 3. SSE Streaming Chat & Embeddable ChatHub <script> Widget ---


@final_router.post("/chat/{workflow_id}/stream")
async def stream_chat_message(
    workflow_id: UUID,
    body: ChatMessageIn,
    request: Request,
    tenant: TenantContext = Depends(require_permission("runs:execute")),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Execute chat workflow and stream response tokens over Server-Sent Events."""
    result = await send_chat_message(
        workflow_id=workflow_id,
        body=body,
        request=request,
        tenant=tenant,
        session=session,
    )
    reply_text = str(result.get("reply", ""))

    async def _gen() -> AsyncIterator[str]:
        words = reply_text.split(" ")
        for idx, word in enumerate(words):
            chunk = word + (" " if idx < len(words) - 1 else "")
            yield f"event: delta\ndata: {json.dumps({'delta': chunk})}\n\n"
        yield f"event: done\ndata: {json.dumps(result)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


@final_router.get("/chat/{workflow_id}/embed.js", response_class=PlainTextResponse)
async def get_embeddable_chat_widget(
    workflow_id: UUID,
    studio_url: str = Query(default="https://flowpilot-web.onrender.com"),
) -> Response:
    """Serve a zero-dependency embeddable floating ChatHub widget script."""
    safe_base = studio_url.rstrip("/")
    chat_iframe_url = f"{safe_base}/chat/{workflow_id}"
    js = f"""(function(){{
  if(document.getElementById('flowpilot-chat-widget')) return;
  var btn=document.createElement('button');
  btn.id='flowpilot-chat-widget';
  btn.textContent='Chat';
  btn.style.cssText='position:fixed;bottom:20px;right:20px;z-index:99999;padding:10px 16px;'
    +'border-radius:999px;border:1px solid #27272a;background:#18181b;color:#fff;'
    +'font:500 13px system-ui,sans-serif;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.22)';
  var box=document.createElement('iframe');
  box.src='{chat_iframe_url}';
  box.style.cssText='display:none;position:fixed;bottom:72px;right:20px;width:380px;height:540px;'
    +'max-width:calc(100vw - 32px);max-height:calc(100vh - 96px);border:1px solid #e4e4e7;'
    +'border-radius:14px;z-index:99999;background:#fff;box-shadow:0 20px 50px rgba(0,0,0,.2)';
  btn.onclick=function(){{box.style.display=box.style.display==='none'?'block':'none';}};
  document.body.appendChild(box);
  document.body.appendChild(btn);
}})();"""
    return Response(content=js, media_type="application/javascript")


# --- 4. 2FA / TOTP Enrollment & Verification ---


class TotpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=6, max_length=8)


@final_router.post("/auth/2fa/enroll")
async def enroll_totp_2fa(
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    secret = generate_totp_secret()
    mfa_row = (
        await session.execute(
            select(UserMfaModel).where(
                UserMfaModel.organization_id == tenant.organization_id,
                UserMfaModel.actor == tenant.actor,
            )
        )
    ).scalar_one_or_none()
    rec_id = mfa_row.id if mfa_row else tenant.organization_id
    envelope = request.app.state.cipher.encrypt_secrets(
        {"totp_secret": secret},
        organization_id=tenant.organization_id,
        credential_id=rec_id,
    )
    if mfa_row is None:
        mfa_row = UserMfaModel(
            id=rec_id,
            organization_id=tenant.organization_id,
            actor=tenant.actor,
            encrypted_secret_envelope=envelope.model_dump(mode="json"),
            enabled=False,
        )
        session.add(mfa_row)
    else:
        mfa_row.encrypted_secret_envelope = envelope.model_dump(mode="json")
        mfa_row.enabled = False
    await session.commit()
    return {
        "secret": secret,
        "otpauth_uri": build_otpauth_uri(secret=secret, account_email=tenant.actor),
        "enabled": False,
    }


@final_router.post("/auth/2fa/verify")
async def verify_totp_2fa(
    body: TotpVerifyRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    mfa_row = (
        await session.execute(
            select(UserMfaModel).where(
                UserMfaModel.organization_id == tenant.organization_id,
                UserMfaModel.actor == tenant.actor,
            )
        )
    ).scalar_one_or_none()
    if mfa_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="2FA is not enrolled for this account.",
        )
    envelope = EncryptedEnvelope.model_validate(mfa_row.encrypted_secret_envelope)
    secrets_map = request.app.state.cipher.decrypt_secrets(
        envelope,
        organization_id=tenant.organization_id,
        credential_id=mfa_row.id,
    )
    if not verify_totp_code(secrets_map["totp_secret"], body.code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid 2FA TOTP verification code.",
        )
    mfa_row.enabled = True
    session.add(
        AuditLogModel(
            organization_id=tenant.organization_id,
            actor=tenant.actor,
            action="auth.2fa.enabled",
            resource_type="user_mfa",
            resource_id=str(mfa_row.id),
        )
    )
    await session.commit()
    return {"verified": True, "enabled": True}


# --- 5. Custom RBAC Roles CRUD ---


class CustomRoleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9_-]+$")
    description: str = ""
    permissions: list[str] = Field(..., min_length=1, max_length=64)


@final_router.get("/roles")
async def list_custom_roles(
    tenant: TenantContext = Depends(require_permission("org:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(CustomRoleModel)
                .where(CustomRoleModel.organization_id == tenant.organization_id)
                .order_by(CustomRoleModel.name.asc())
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
            "permissions": r.permissions,
        }
        for r in rows
    ]


@final_router.post("/roles", status_code=status.HTTP_201_CREATED)
async def create_custom_role(
    body: CustomRoleCreateRequest,
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    row = CustomRoleModel(
        organization_id=tenant.organization_id,
        name=body.name,
        description=body.description,
        permissions=sorted(set(body.permissions)),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "permissions": row.permissions,
    }


@final_router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_role(
    role_id: UUID,
    tenant: TenantContext = Depends(require_permission("org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await session.get(CustomRoleModel, role_id)
    if row is None or row.organization_id != tenant.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Custom role not found.")
    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- 6. Enterprise LDAP Directory Login ---


class LdapLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    username: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=256)


@final_router.post("/sso/ldap/login")
async def sso_ldap_login(
    body: LdapLoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Authenticate against a configured enterprise LDAP directory binder and JIT provision."""
    authenticator = getattr(request.app.state, "ldap_authenticator", None)
    if authenticator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LDAP directory authenticator is not configured on this instance.",
        )
    directory_user = await authenticator(body.username, body.password)
    if not directory_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid LDAP directory credentials.",
        )
    groups = directory_user.get("groups", [])
    role = _map_groups_to_role(groups if isinstance(groups, list) else [])
    email = str(directory_user.get("email") or f"{body.username}@ldap.local")
    user = await _jit_provision_sso_user(
        session,
        organization_id=body.organization_id,
        email=email,
        full_name=str(directory_user.get("full_name", body.username)),
        role=role,
        provider="ldap",
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
        "provider": "ldap",
    }
