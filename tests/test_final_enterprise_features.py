"""Tests for Error Workflow auto-dispatch, Collaborative Edit Locks, PDF RAG Upload,
SSE Streaming Chat, Embeddable Chat Widget, 2FA/TOTP, Custom RBAC Roles, LDAP SSO,
Azure Key Vault KMS, and LLM-backed AI Assistant."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from flowpilot_api import Settings, create_app
from flowpilot_credentials import AzureKeyVaultKeyProvider, EnvelopeCipher
from flowpilot_nodes_core import ModelCompletion
from flowpilot_policy import (
    PolicyDeniedError,
    authorize,
    generate_totp_code,
    generate_totp_secret,
    verify_totp_code,
)


class _StubAzureCryptoClient:
    def wrap_key(self, algorithm: str, key: bytes) -> Any:
        return SimpleNamespace(encrypted_key=b"az:" + key[::-1])

    def unwrap_key(self, algorithm: str, encrypted_key: bytes) -> Any:
        assert encrypted_key.startswith(b"az:")
        return SimpleNamespace(key=encrypted_key[3:][::-1])


class _StubAssistantLlm:
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
        return ModelCompletion(
            content="LLM advice: consider adding retry backoff on your HTTP step.",
            input_tokens=20,
            output_tokens=15,
            cost_usd=0.0001,
        )


def test_azure_key_vault_kms_and_totp_and_custom_rbac() -> None:
    kms = AzureKeyVaultKeyProvider(
        vault_url="https://acme-vault.vault.azure.net",
        key_name="flowpilot-kek",
        crypto_client=_StubAzureCryptoClient(),
    )
    cipher = EnvelopeCipher(kms)
    org_id, cred_id = uuid4(), uuid4()
    env = cipher.encrypt_secrets(
        {"secret": "az-top-secret"}, organization_id=org_id, credential_id=cred_id
    )
    assert env.key_id.startswith("azure-kv:")
    assert cipher.decrypt_secrets(env, organization_id=org_id, credential_id=cred_id) == {
        "secret": "az-top-secret"
    }

    secret = generate_totp_secret()
    code = generate_totp_code(secret, for_time=1_700_000_000)
    assert verify_totp_code(secret, code, for_time=1_700_000_000) is True
    assert verify_totp_code(secret, "000000", for_time=1_700_000_000) is False

    authorize(
        custom_permissions=["workflows:read", "runs:execute"],
        required_permission="runs:execute",
    )
    with pytest.raises(PolicyDeniedError):
        authorize(custom_permissions=["workflows:read"], required_permission="org:manage")


@pytest.mark.asyncio
async def test_error_workflow_locks_pdf_rag_sse_chat_embed_2fa_roles_ldap_and_llm_assistant(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "final_enterprise.db"

    def _fail_http(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad upstream request")

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        auto_create_tables=True,
        master_key="final-test-master-key",
        session_secret="final-test-session-secret",
    )
    app = create_app(
        settings,
        http_transport=httpx.MockTransport(_fail_http),
        agent_model_provider=_StubAssistantLlm(),
    )

    async def _ldap_auth(username: str, password: str) -> dict[str, Any] | None:
        if username == "jdoe" and password == "corp-secret":
            return {
                "email": "jdoe@corp.io",
                "full_name": "Jane Doe",
                "groups": ["admins"],
            }
        return None

    app.state.ldap_authenticator = _ldap_auth

    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        boot = (
            await client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "email": "owner@final.io",
                    "organization_name": "Final Enterprise",
                    "organization_slug": "final-enterprise",
                },
            )
        ).json()
        headers = {"Authorization": f"Bearer {boot['api_key']}"}
        proj_id = boot["project_id"]
        org_id = boot["organization_id"]

        # 1. Error Workflow auto-dispatch on run failure
        err_wf = (
            await client.post(
                "/api/v1/workflows",
                headers=headers,
                json={
                    "project_id": proj_id,
                    "name": "Global Error Handler",
                    "graph": {
                        "nodes": [
                            {"id": "err", "type": "core.error_trigger"},
                            {
                                "id": "record",
                                "type": "core.data_table",
                                "config": {
                                    "table_name": "error_incidents",
                                    "operation": "insert",
                                    "data": {"msg": "{{ nodes.err.output.error_message }}"},
                                },
                            },
                        ],
                        "edges": [{"id": "e1", "source": "err", "target": "record"}],
                    },
                },
            )
        ).json()
        err_pub = (
            await client.post(
                f"/api/v1/workflows/{err_wf['id']}/publish",
                headers=headers,
                json={"changelog": "err v1", "activate": True},
            )
        ).json()

        failing_wf = (
            await client.post(
                "/api/v1/workflows",
                headers=headers,
                json={
                    "project_id": proj_id,
                    "name": "Flaky Workflow",
                    "settings": {"error_workflow_version_id": err_pub["id"]},
                    "graph": {
                        "nodes": [
                            {"id": "s", "type": "core.manual_trigger"},
                            {
                                "id": "bad_http",
                                "type": "core.http_request",
                                "config": {"method": "GET", "url": "https://api.example.com/fail"},
                            },
                        ],
                        "edges": [{"id": "e1", "source": "s", "target": "bad_http"}],
                    },
                },
            )
        ).json()
        await client.post(
            f"/api/v1/workflows/{failing_wf['id']}/publish",
            headers=headers,
            json={"changelog": "v1", "activate": True},
        )
        failed_run = (
            await client.post(
                f"/api/v1/workflows/{failing_wf['id']}/runs",
                headers=headers,
                json={"trigger_input": {}},
            )
        ).json()
        assert failed_run["status"] == "FAILED"

        # Verify Error Workflow actually executed and logged into error_incidents Data Table!
        tables = (await client.get("/api/v1/tables", headers=headers)).json()
        inc_table = next(t for t in tables if t["name"] == "error_incidents")
        inc_rows = (
            await client.get(f"/api/v1/tables/{inc_table['id']}/rows", headers=headers)
        ).json()
        assert len(inc_rows) == 1

        # 2. Collaborative Edit Locks (with 409 Conflict across two actors)
        ldap_login = await client.post(
            "/api/v1/sso/ldap/login",
            json={"organization_id": org_id, "username": "jdoe", "password": "corp-secret"},
        )
        assert ldap_login.status_code == 200
        assert ldap_login.json()["role"] == "admin"
        ldap_headers = {"Authorization": f"Bearer {ldap_login.json()['access_token']}"}

        # Bad LDAP password rejected
        assert (
            await client.post(
                "/api/v1/sso/ldap/login",
                json={"organization_id": org_id, "username": "jdoe", "password": "wrong"},
            )
        ).status_code == 401

        lk1 = await client.post(
            f"/api/v1/workflows/{failing_wf['id']}/lock",
            headers=headers,
            json={"ttl_seconds": 120},
        )
        assert lk1.status_code == 200
        assert lk1.json()["is_owner"] is True

        # Second actor gets 409 Conflict while locked
        lk_conflict = await client.post(
            f"/api/v1/workflows/{failing_wf['id']}/lock",
            headers=ldap_headers,
            json={"ttl_seconds": 120},
        )
        assert lk_conflict.status_code == 409

        # Release and re-acquire cleanly
        assert (
            await client.delete(f"/api/v1/workflows/{failing_wf['id']}/lock", headers=headers)
        ).status_code == 204
        lk2 = await client.post(
            f"/api/v1/workflows/{failing_wf['id']}/lock",
            headers=ldap_headers,
            json={"ttl_seconds": 60},
        )
        assert lk2.status_code == 200

        # 3. PDF RAG Upload + Extraction + Vector Search
        minimal_pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Length 64 >>\nstream\n"
            b"BT (Enterprise SLA uptime guarantee is 99.99 percent) Tj ET\n"
            b"endstream\nendobj\n%%EOF"
        )
        up_pdf = await client.post(
            "/api/v1/knowledge/upload",
            headers=headers,
            json={
                "filename": "sla.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(minimal_pdf).decode("ascii"),
            },
        )
        assert up_pdf.status_code == 201
        assert up_pdf.json()["chunk_count"] >= 1
        rag_hit = (
            await client.post(
                "/api/v1/knowledge/search",
                headers=headers,
                json={"query": "99.99 percent SLA", "top_k": 2},
            )
        ).json()
        assert "99.99" in rag_hit["chunks"][0]["content"]

        # 4. SSE Streaming Chat & Embeddable Chat Widget
        chat_wf = (
            await client.post(
                "/api/v1/workflows",
                headers=headers,
                json={
                    "project_id": proj_id,
                    "name": "Streaming Bot",
                    "graph": {
                        "nodes": [
                            {"id": "c", "type": "core.chat_trigger"},
                            {
                                "id": "r",
                                "type": "core.set_data",
                                "config": {"values": {"reply": "Hello from streaming FlowPilot"}},
                            },
                        ],
                        "edges": [{"id": "e1", "source": "c", "target": "r"}],
                    },
                },
            )
        ).json()
        await client.post(
            f"/api/v1/workflows/{chat_wf['id']}/publish",
            headers=headers,
            json={"changelog": "v1", "activate": True},
        )
        stream_resp = await client.post(
            f"/api/v1/chat/{chat_wf['id']}/stream",
            headers=headers,
            json={"message": "Hi"},
        )
        assert stream_resp.status_code == 200
        assert "event: delta" in stream_resp.text
        assert "event: done" in stream_resp.text

        embed_js = await client.get(f"/api/v1/chat/{chat_wf['id']}/embed.js")
        assert embed_js.status_code == 200
        assert f"/chat/{chat_wf['id']}" in embed_js.text

        # 5. 2FA / TOTP Enrollment & Verification
        enrolled = (await client.post("/api/v1/auth/2fa/enroll", headers=headers)).json()
        assert enrolled["otpauth_uri"].startswith("otpauth://totp/")
        assert (
            await client.post("/api/v1/auth/2fa/verify", headers=headers, json={"code": "000000"})
        ).status_code == 401
        valid_totp = generate_totp_code(enrolled["secret"])
        verified = await client.post(
            "/api/v1/auth/2fa/verify",
            headers=headers,
            json={"code": valid_totp},
        )
        assert verified.status_code == 200
        assert verified.json()["enabled"] is True

        # 6. Custom RBAC Roles CRUD
        role_resp = await client.post(
            "/api/v1/roles",
            headers=headers,
            json={
                "name": "release_manager",
                "description": "Can publish workflows and decide approvals",
                "permissions": ["workflows:publish", "approvals:decide"],
            },
        )
        assert role_resp.status_code == 201
        role_id = role_resp.json()["id"]
        assert len((await client.get("/api/v1/roles", headers=headers)).json()) == 1
        assert (
            await client.delete(f"/api/v1/roles/{role_id}", headers=headers)
        ).status_code == 204

        # 7. LLM-Backed AI Assistant
        assist_resp = (
            await client.post(
                "/api/v1/ai/assistant",
                headers=headers,
                json={
                    "message": "How can I make my workflow more resilient?",
                    "workflow_name": "Resilience Check",
                    "graph": {
                        "nodes": [{"id": "s", "type": "core.manual_trigger"}],
                        "edges": [],
                    },
                },
            )
        ).json()
        assert "LLM advice" in assist_resp["reply"]
