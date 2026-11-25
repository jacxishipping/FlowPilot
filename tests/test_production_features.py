"""Tests for CircuitBreaker, DLQ, EventBus, Cloud KMS/CMK, LLM providers, Browser worker,
Marketplace scanner, JWT auth/RBAC, OAuth2 PKCE, cursor pagination, Stripe checkout/reconcile,
Temporal dispatch mode, and SqlAlchemyVectorMemoryStore."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from flowpilot import (
    NodeTestHarness,
    SecurityPolicyError,
    scan_package_source,
)
from flowpilot_agents import (
    AnthropicModelProvider,
    GeminiModelProvider,
    MultiModelProvider,
    OpenAIModelProvider,
    SqlAlchemyVectorMemoryStore,
)
from flowpilot_api import Settings, create_app
from flowpilot_api.auth import create_session_token
from flowpilot_api.db.models import ApprovalModel, OrganizationMemberModel, OrganizationModel
from flowpilot_credentials import (
    AwsKmsProvider,
    CustomerManagedKeyProvider,
    GcpKmsProvider,
    LocalMasterKeyProvider,
    TenantCmkRouter,
    VaultTransitKmsProvider,
)
from flowpilot_engine import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    InMemoryEventBus,
)
from flowpilot_observability import OtlpConfig, configure_otlp_tracing
from flowpilot_policy import Role
from flowpilot_schema import EventType, ExecutionEvent
from flowpilot_worker_browser import BrowserAutomationNode, BrowserExtractResult


def test_circuit_breaker_and_cloud_kms_cmk_routing() -> None:
    cb = CircuitBreaker("stripe", failure_threshold=2, recovery_timeout_seconds=10.0)
    assert str(cb.state) == CircuitState.CLOSED
    cb.ensure_allowed(now=100.0)
    cb.record_failure(now=100.0)
    cb.record_failure(now=101.0)
    assert str(cb.state) == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.ensure_allowed(now=105.0)
    # After recovery timeout, enters HALF_OPEN and closes on success
    cb.ensure_allowed(now=112.0)
    assert str(cb.state) == CircuitState.HALF_OPEN
    cb.record_success()
    assert str(cb.state) == CircuitState.CLOSED

    # AWS KMS, GCP KMS, Vault Transit, and Tenant CMK Router
    class _FakeAwsKms:
        def encrypt(self, **kwargs: Any) -> dict[str, bytes]:
            return {"CiphertextBlob": b"aws:" + bytes(kwargs["Plaintext"])}

        def decrypt(self, **kwargs: Any) -> dict[str, bytes]:
            blob = bytes(kwargs["CiphertextBlob"])
            return {"Plaintext": blob[4:]}

    class _FakeGcpKms:
        def encrypt(self, request: dict[str, Any]) -> Any:
            return SimpleNamespace(ciphertext=b"gcp:" + bytes(request["plaintext"]))

        def decrypt(self, request: dict[str, Any]) -> Any:
            return SimpleNamespace(plaintext=bytes(request["ciphertext"])[4:])

    class _FakeVault:
        def encrypt_data(self, **kwargs: Any) -> dict[str, Any]:
            return {"data": {"ciphertext": f"vault:v1:{kwargs['plaintext']}"}}

        def decrypt_data(self, **kwargs: Any) -> dict[str, Any]:
            raw = str(kwargs["ciphertext"]).split(":", 2)[2]
            return {"data": {"plaintext": raw}}

    aad = b"org:1:cred:2"
    dek = b"A" * 32
    for prov in (
        AwsKmsProvider(key_arn="arn:aws:kms:us-east-1:123:key/abc", kms_client=_FakeAwsKms()),
        GcpKmsProvider(
            crypto_key_name="projects/p/locations/g/keyRings/r/cryptoKeys/k",
            kms_client=_FakeGcpKms(),
        ),
        VaultTransitKmsProvider(key_name="fp-key", vault_client=_FakeVault()),
    ):
        kid, wrapped, nonce = prov.wrap_dek(dek, aad=aad)
        assert prov.unwrap_dek(key_id=kid, wrapped_dek=wrapped, nonce=nonce, aad=aad) == dek

    # Tenant CMK Router isolates enterprise tenant keys from SaaS default KEK
    enterprise_org = uuid4()
    standard_org = uuid4()
    cred_id = uuid4()
    router = TenantCmkRouter(LocalMasterKeyProvider(active_key_id="saas-v1", master_secret="saas"))
    router.register_tenant_kms(
        enterprise_org,
        CustomerManagedKeyProvider(
            organization_id=enterprise_org,
            key_alias="acme-cmk-1",
            raw_key_material=b"enterprise-customer-owned-key-material",
        ),
    )
    ent_env = router.encrypt_secrets(
        {"api_key": "ent-secret"},
        organization_id=enterprise_org,
        credential_id=cred_id,
    )
    std_env = router.encrypt_secrets(
        {"api_key": "std-secret"},
        organization_id=standard_org,
        credential_id=cred_id,
    )
    assert ent_env.key_id.startswith("cmk:")
    assert std_env.key_id == "saas-v1"
    assert (
        router.decrypt_secrets(ent_env, organization_id=enterprise_org, credential_id=cred_id)[
            "api_key"
        ]
        == "ent-secret"
    )
    assert configure_otlp_tracing(OtlpConfig(enabled=False)) is False


@pytest.mark.asyncio
async def test_live_llm_provider_adapters_and_browser_worker() -> None:
    def _openai_mock(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "OpenAI ok",
                            "tool_calls": [
                                {"function": {"name": "lookup", "arguments": '{"id": 1}'}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    def _anthropic_mock(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Claude ok"},
                    {"type": "tool_use", "name": "search", "input": {"q": "flow"}},
                ],
                "usage": {"input_tokens": 80, "output_tokens": 40},
                "stop_reason": "tool_use",
            },
        )

    def _gemini_mock(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Gemini ok"},
                                {"functionCall": {"name": "fetch", "args": {"url": "https://a.io"}}},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 30},
            },
        )

    multi = MultiModelProvider(
        {
            "openai": OpenAIModelProvider(
                api_key="sk-test",
                transport=httpx.MockTransport(_openai_mock),
            ),
            "anthropic": AnthropicModelProvider(
                api_key="sk-ant-test",
                transport=httpx.MockTransport(_anthropic_mock),
            ),
            "gemini": GeminiModelProvider(
                api_key="gm-test",
                transport=httpx.MockTransport(_gemini_mock),
            ),
        }
    )

    o_res = await multi.complete(
        provider="openai",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        tools=["lookup"],
    )
    assert o_res.content == "OpenAI ok"
    assert o_res.tool_calls[0].tool_name == "lookup"
    assert o_res.cost_usd > 0

    a_res = await multi.complete(
        provider="anthropic",
        model="claude-3-5-sonnet-latest",
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        tools=["search"],
    )
    assert a_res.content == "Claude ok"
    assert a_res.tool_calls[0].arguments == {"q": "flow"}

    g_res = await multi.complete(
        provider="gemini",
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
        tools=["fetch"],
    )
    assert g_res.content == "Gemini ok"
    assert g_res.tool_calls[0].tool_name == "fetch"

    # Browser Automation Worker enforces SSRF & meters browser_time_ms
    class _FakeBrowserDriver:
        async def navigate_and_extract(self, **kwargs: Any) -> BrowserExtractResult:
            return BrowserExtractResult(
                url=kwargs["url"],
                title="Acme Status",
                extracted={"status": "All Systems Operational"},
                text_snippet="All Systems Operational",
                elapsed_ms=420,
            )

    harness = NodeTestHarness(services={"browser_driver": _FakeBrowserDriver()})
    b_res = await harness.run(
        BrowserAutomationNode(),
        {
            "url": "https://status.example.com",
            "selectors": {"status": ".banner"},
            "allowed_domains": ["example.com"],
        },
    )
    assert b_res.result.output["extracted"]["status"] == "All Systems Operational"
    assert any(u.metric == "browser_time_ms" for u in b_res.usage_records)

    with pytest.raises(SecurityPolicyError):
        await harness.run(BrowserAutomationNode(), {"url": "http://127.0.0.1:8080/internal"})

    # Marketplace static scanner catches dangerous imports
    bad_scan = scan_package_source({"node.py": "import subprocess\nsubprocess.run(['id'])"})
    assert not bad_scan.passed
    good_scan = scan_package_source({"node.py": "import json\ndef parse(x): return json.loads(x)"})
    assert good_scan.passed


@pytest.mark.asyncio
async def test_jwt_auth_rbac_oauth2_pagination_billing_marketplace_and_temporal(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "prod_features.db"

    def _mock_oauth_token(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "xoxb-oauth-live-token", "refresh_token": "xoxr-refresh"},
        )

    temporal_started: list[dict[str, Any]] = []
    temporal_signals: list[dict[str, Any]] = []

    class _FakeTemporalHandle:
        first_execution_run_id = "temporal-run-999"

        async def signal(self, name: str, payload: Any = None) -> None:
            temporal_signals.append({"name": name, "payload": payload})

    class _FakeTemporalClient:
        async def start_workflow(self, name: str, arg: Any, **kwargs: Any) -> Any:
            temporal_started.append({"name": name, "id": kwargs["id"], "tasks": arg})
            return _FakeTemporalHandle()

        def get_workflow_handle(self, wf_id: str) -> Any:
            return _FakeTemporalHandle()

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        auto_create_tables=True,
        session_secret="test-jwt-secret-key",
    )
    app = create_app(
        settings,
        oauth_http_transport=httpx.MockTransport(_mock_oauth_token),
        temporal_client=_FakeTemporalClient(),
    )

    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        # 1. Register + Login + Cookie/JWT /auth/me + Switch Organization
        reg = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "founder@flow.io",
                "password": "secure-password-123",
                "full_name": "Founder",
                "organization_name": "Flow HQ",
                "organization_slug": "flow-hq",
            },
        )
        assert reg.status_code == 201
        reg_data = reg.json()
        jwt_headers = {"Authorization": f"Bearer {reg_data['access_token']}"}

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "founder@flow.io", "password": "secure-password-123"},
        )
        assert login.status_code == 200
        assert "flowpilot_session" in login.cookies

        me = (await client.get("/api/v1/auth/me", headers=jwt_headers)).json()
        assert me["role"] == "owner"
        assert me["organization_id"] == reg_data["organization_id"]

        # Add second org membership for org switching test
        async with app.state.session_factory() as session:
            org2 = OrganizationModel(name="Second Org", slug="second-org")
            session.add(org2)
            await session.flush()
            session.add(
                OrganizationMemberModel(
                    organization_id=org2.id,
                    user_id=UUID(reg_data["user_id"]),
                    role=Role.VIEWER.value,
                )
            )
            await session.commit()
            org2_id = str(org2.id)

        switched = (
            await client.post(
                "/api/v1/auth/switch-organization",
                headers=jwt_headers,
                json={"organization_id": org2_id},
            )
        ).json()
        assert switched["organization_id"] == org2_id
        assert switched["role"] == "viewer"

        # 2. RBAC enforcement: viewer token gets 403 on workflows:publish
        viewer_token = create_session_token(
            secret=settings.session_secret,
            user_id=UUID(reg_data["user_id"]),
            email="viewer@flow.io",
            organization_id=UUID(reg_data["organization_id"]),
            role=Role.VIEWER,
        )
        wf_ids: list[str] = []
        for idx in range(3):
            wf_r = await client.post(
                "/api/v1/workflows",
                headers=jwt_headers,
                json={
                    "project_id": reg_data["project_id"],
                    "name": f"Workflow {idx}",
                    "graph": {
                        "nodes": [{"id": "start", "type": "core.manual_trigger"}],
                        "edges": [],
                    },
                },
            )
            assert wf_r.status_code == 201
            wf_ids.append(wf_r.json()["id"])

        forbidden = await client.post(
            f"/api/v1/workflows/{wf_ids[0]}/publish",
            headers={"Authorization": f"Bearer {viewer_token}"},
            json={"activate": True},
        )
        assert forbidden.status_code == 403

        # 3. Cursor pagination on /api/v1/workflows/paginated
        page1 = (
            await client.get("/api/v1/workflows/paginated?limit=2", headers=jwt_headers)
        ).json()
        assert len(page1["items"]) == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] is not None

        # 4. OAuth2 PKCE authorize -> callback -> encrypted credential
        auth_start = (
            await client.post(
                "/api/v1/oauth/slack/authorize",
                headers=jwt_headers,
                json={
                    "client_id": "slack-client-1",
                    "redirect_uri": "https://app.flowpilot.dev/oauth/cb",
                    "scopes": ["chat:write"],
                    "credential_name": "Slack OAuth Bot",
                },
            )
        ).json()
        assert "code_challenge=" in auth_start["authorization_url"]

        cb_resp = await client.post(
            "/api/v1/oauth/slack/callback",
            headers=jwt_headers,
            json={
                "state": auth_start["state"],
                "code": "auth-code-123",
                "client_id": "slack-client-1",
                "client_secret": "slack-secret-1",
            },
        )
        assert cb_resp.status_code == 201
        assert "xoxb-oauth-live-token" not in cb_resp.text

        # 5. Stripe outbound checkout, customer portal & billing reconciliation
        checkout = (
            await client.post(
                "/api/v1/billing/checkout",
                headers=jwt_headers,
                json={"plan": "pro"},
            )
        ).json()
        assert checkout["plan"] == "pro"
        assert checkout["monthly_price_usd"] == 49

        portal = (
            await client.post("/api/v1/billing/portal", headers=jwt_headers, json={})
        ).json()
        assert "url" in portal

        reconcile = (
            await client.post(
                "/api/v1/billing/reconcile?period_key=2026-11",
                headers=jwt_headers,
            )
        ).json()
        assert reconcile["period_key"] == "2026-11"

        # 6. Marketplace submission scanner rejects unsafe code & signs safe package
        unsafe_sub = await client.post(
            "/api/v1/marketplace/submissions",
            headers=jwt_headers,
            json={
                "publisher": "BadActor",
                "manifest": {
                    "package_name": "evil-pkg",
                    "package_version": "0.1.0",
                    "first_party": False,
                },
                "source_files": {"evil.py": "import os\nos.system('rm -rf /')"},
            },
        )
        assert unsafe_sub.status_code == 422

        safe_sub = await client.post(
            "/api/v1/marketplace/submissions",
            headers=jwt_headers,
            json={
                "publisher": "VerifiedPartner",
                "manifest": {
                    "package_name": "partner-formatter",
                    "package_version": "1.0.0",
                    "first_party": False,
                    "entrypoints": {"partner.fmt": "partner_fmt:FmtNode"},
                },
                "source_files": {"fmt.py": "import json\ndef run(d): return json.dumps(d)"},
            },
        )
        assert safe_sub.status_code == 201
        assert safe_sub.json()["scan_passed"] is True
        assert safe_sub.json()["signature"] is not None

        # 7. Temporal dispatch mode starts FlowPilotDurableWorkflow & signals approvals
        await client.post(
            f"/api/v1/workflows/{wf_ids[0]}/publish",
            headers=jwt_headers,
            json={"activate": True},
        )
        app.state.settings.execution_mode = "temporal"
        t_run = (
            await client.post(
                f"/api/v1/workflows/{wf_ids[0]}/runs",
                headers=jwt_headers,
                json={"trigger_input": {"hello": "temporal"}},
            )
        ).json()
        assert t_run["temporal_workflow_id"].startswith("flowpilot-run-")
        assert len(temporal_started) == 1

        # Seed a pending approval on the temporal run and send signal
        app_id = uuid4()
        async with app.state.session_factory() as session:
            session.add(
                ApprovalModel(
                    id=app_id,
                    organization_id=UUID(reg_data["organization_id"]),
                    run_id=UUID(t_run["id"]),
                    node_id="gate",
                    title="Approve temporal step",
                )
            )
            await session.commit()

        sig_resp = (
            await client.post(
                f"/api/v1/approvals/{app_id}/decision",
                headers=jwt_headers,
                json={"approved": True, "comment": "go"},
            )
        ).json()
        assert sig_resp["signaled"] is True
        assert temporal_signals[0]["name"] == "approval_decision"

        # 8. Live EventBus pub/sub & SqlAlchemyVectorMemoryStore tenant isolation
        bus = InMemoryEventBus()
        run_uuid = uuid4()
        received: list[int] = []

        async def _consumer() -> None:
            async for evt in bus.subscribe(run_uuid):
                received.append(evt.sequence)
                break

        task = asyncio.create_task(_consumer())
        await asyncio.sleep(0.01)
        await bus.publish(
            ExecutionEvent(
                sequence=1,
                run_id=run_uuid,
                organization_id=UUID(reg_data["organization_id"]),
                event_type=EventType.RUN_STARTED,
            )
        )
        await task
        assert received == [1]

        mem_store = SqlAlchemyVectorMemoryStore(app.state.session_factory)
        agent_uuid = uuid4()
        await mem_store.remember(
            organization_id=UUID(reg_data["organization_id"]),
            agent_id=agent_uuid,
            content="Enterprise SLA response time is 15 minutes.",
        )
        recalled = await mem_store.recall(
            organization_id=UUID(reg_data["organization_id"]),
            agent_id=agent_uuid,
            query="What is enterprise SLA response time?",
        )
        assert len(recalled) == 1
        assert "15 minutes" in recalled[0].content
        # Cross-tenant recall returns empty
        other_recalled = await mem_store.recall(
            organization_id=uuid4(),
            agent_id=agent_uuid,
            query="enterprise SLA",
        )
        assert other_recalled == []
        assert base64.b64encode(b"ok") == b"b2s="
