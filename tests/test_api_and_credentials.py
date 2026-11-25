"""Integration tests for envelope encryption, FastAPI /api/v1, RLS, SSE, and Temporal worker."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from flowpilot_api import Settings, create_app
from flowpilot_credentials import (
    CredentialEncryptionError,
    EnvelopeCipher,
    LocalMasterKeyProvider,
)
from flowpilot_engine import ActivityTask, PayloadManager
from flowpilot_nodes_core import create_default_registry
from flowpilot_schema import PayloadRef, WorkerPool
from flowpilot_worker_workflow import NodeActivityExecutor


def test_envelope_encryption_tenant_aad_binding_and_key_rotation() -> None:
    kms_v1 = LocalMasterKeyProvider(
        active_key_id="kms-v1",
        keys={"kms-v1": "secret-kek-version-1", "kms-v2": "secret-kek-version-2"},
    )
    cipher_v1 = EnvelopeCipher(kms_v1)

    org_a = uuid4()
    org_b = uuid4()
    cred_id = uuid4()
    secrets = {"token": "sk-live-secret-12345", "refresh": "ref-999"}

    env1 = cipher_v1.encrypt_secrets(secrets, organization_id=org_a, credential_id=cred_id)
    env2 = cipher_v1.encrypt_secrets(secrets, organization_id=org_a, credential_id=cred_id)
    assert env1.ciphertext_b64 != env2.ciphertext_b64  # Fresh random DEK + nonce per write
    assert env1.key_id == "kms-v1"

    assert cipher_v1.decrypt_secrets(env1, organization_id=org_a, credential_id=cred_id) == secrets

    # Cross-tenant AAD mismatch must fail authenticated decryption
    with pytest.raises(CredentialEncryptionError):
        cipher_v1.decrypt_secrets(env1, organization_id=org_b, credential_id=cred_id)

    # Rotate KEK from kms-v1 -> kms-v2 without exposing plaintext
    kms_v2 = LocalMasterKeyProvider(
        active_key_id="kms-v2",
        keys={"kms-v1": "secret-kek-version-1", "kms-v2": "secret-kek-version-2"},
    )
    cipher_v2 = EnvelopeCipher(kms_v2)
    rotated = cipher_v2.rewrap_envelope(env1, organization_id=org_a, credential_id=cred_id)
    assert rotated.key_id == "kms-v2"
    assert (
        cipher_v2.decrypt_secrets(rotated, organization_id=org_a, credential_id=cred_id) == secrets
    )


@pytest.mark.asyncio
async def test_api_v1_end_to_end_multi_tenant_approvals_credentials_sse_and_ai(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "flowpilot_test.db"
    seen_auth_header: list[str] = []

    def _mock_crm(request: httpx.Request) -> httpx.Response:
        seen_auth_header.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"customer": "Globex", "tier": "enterprise"})

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        auto_create_tables=True,
        master_key="test-master-key-for-api-integration",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(_mock_crm))

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Health, node catalog, and canonical JSON Schema
            health = await client.get("/health")
            assert health.status_code == 200
            assert "x-request-id" in health.headers

            catalog = (await client.get("/api/v1/nodes/catalog")).json()
            assert len(catalog["nodes"]) == 25

            wf_schema = (await client.get("/api/v1/schemas/workflow")).json()
            assert wf_schema["title"] == "WorkflowDefinition"

            # 2. Bootstrap two isolated tenants (Org A & Org B)
            boot_a = (
                await client.post(
                    "/api/v1/auth/bootstrap",
                    json={
                        "email": "alice@orga.io",
                        "organization_name": "Org A",
                        "organization_slug": "org-a",
                    },
                )
            ).json()
            boot_b = (
                await client.post(
                    "/api/v1/auth/bootstrap",
                    json={
                        "email": "bob@orgb.io",
                        "organization_name": "Org B",
                        "organization_slug": "org-b",
                    },
                )
            ).json()
            headers_a = {"Authorization": f"Bearer {boot_a['api_key']}"}
            headers_b = {"Authorization": f"Bearer {boot_b['api_key']}"}

            # 3. Store envelope-encrypted credential for Org A (never returns raw token)
            cred_resp = await client.post(
                "/api/v1/credentials",
                headers=headers_a,
                json={
                    "name": "CRM Token",
                    "credential_type": "http_auth",
                    "secrets": {"token": "crm-live-secret-abc"},
                },
            )
            assert cred_resp.status_code == 201
            cred_data = cred_resp.json()
            assert "crm-live-secret-abc" not in cred_resp.text
            cred_id = cred_data["id"]

            # 4. Create workflow using encrypted credential + human approval gate
            wf_resp = await client.post(
                "/api/v1/workflows",
                headers=headers_a,
                json={
                    "project_id": boot_a["project_id"],
                    "name": "CRM Approval Flow",
                    "graph": {
                        "nodes": [
                            {"id": "start", "type": "core.manual_trigger"},
                            {
                                "id": "fetch",
                                "type": "core.http_request",
                                "config": {
                                    "method": "GET",
                                    "url": "https://crm.example.com/v1/account",
                                },
                                "credentials": {"auth": cred_id},
                            },
                            {
                                "id": "approve",
                                "type": "core.human_approval",
                                "config": {
                                    "title": "Approve {{ nodes.fetch.output.body.customer }}",
                                    "fail_on_reject": False,
                                },
                            },
                            {
                                "id": "done",
                                "type": "core.set_data",
                                "config": {
                                    "values": {
                                        "customer": "{{ nodes.fetch.output.body.customer }}",
                                        "approved": "{{ nodes.approve.output.approved }}",
                                    }
                                },
                            },
                        ],
                        "edges": [
                            {"id": "e1", "source": "start", "target": "fetch"},
                            {"id": "e2", "source": "fetch", "target": "approve"},
                            {
                                "id": "e3",
                                "source": "approve",
                                "target": "done",
                                "kind": "true",
                            },
                        ],
                    },
                },
            )
            assert wf_resp.status_code == 201
            wf = wf_resp.json()
            wf_id = wf["id"]
            assert wf["revision"] == 1

            # 5. Tenant isolation: Org B cannot read Org A's workflow
            org_b_wf = await client.get(f"/api/v1/workflows/{wf_id}", headers=headers_b)
            assert org_b_wf.status_code == 404

            # 6. Optimistic concurrency: stale revision rejected with 409 Conflict
            ok_patch = await client.patch(
                f"/api/v1/workflows/{wf_id}",
                headers=headers_a,
                json={"expected_revision": 1, "description": "Updated description"},
            )
            assert ok_patch.status_code == 200
            assert ok_patch.json()["revision"] == 2

            stale_patch = await client.patch(
                f"/api/v1/workflows/{wf_id}",
                headers=headers_a,
                json={"expected_revision": 1, "description": "Stale overwrite"},
            )
            assert stale_patch.status_code == 409

            # 7. Validate & Publish immutable version
            val = (
                await client.post(f"/api/v1/workflows/{wf_id}/validate", headers=headers_a)
            ).json()
            assert val["issues"] == []

            pub = await client.post(
                f"/api/v1/workflows/{wf_id}/publish",
                headers=headers_a,
                json={"changelog": "v1 launch", "activate": True},
            )
            assert pub.status_code == 201
            assert pub.json()["version_number"] == 1

            # 8. Start run with Idempotency-Key -> pauses at WAITING_APPROVAL after CRM HTTP call
            run_resp = await client.post(
                f"/api/v1/workflows/{wf_id}/runs",
                headers={**headers_a, "Idempotency-Key": "run-key-001"},
                json={"trigger_input": {"source": "api"}},
            )
            assert run_resp.status_code == 201
            run = run_resp.json()
            assert run["status"] == "WAITING_APPROVAL"
            assert seen_auth_header == ["Bearer crm-live-secret-abc"]

            # Duplicate Idempotency-Key returns same run without re-executing
            dup_run = (
                await client.post(
                    f"/api/v1/workflows/{wf_id}/runs",
                    headers={**headers_a, "Idempotency-Key": "run-key-001"},
                    json={"trigger_input": {"source": "api"}},
                )
            ).json()
            assert dup_run["id"] == run["id"]

            # 9. Stream SSE execution events
            sse_resp = await client.get(f"/api/v1/runs/{run['id']}/events", headers=headers_a)
            assert sse_resp.status_code == 200
            assert "text/event-stream" in sse_resp.headers["content-type"]
            assert "event: approval.requested" in sse_resp.text

            # 10. Org B cannot decide Org A's approval; Org A approves and resumes run to SUCCEEDED
            approval_id = next(iter(run["approvals"].keys()))
            assert (
                await client.post(
                    f"/api/v1/approvals/{approval_id}/decision",
                    headers=headers_b,
                    json={"approved": True},
                )
            ).status_code == 404

            resumed = (
                await client.post(
                    f"/api/v1/approvals/{approval_id}/decision",
                    headers=headers_a,
                    json={"approved": True, "comment": "Approved by finance"},
                )
            ).json()
            assert resumed["status"] == "SUCCEEDED"
            assert resumed["node_executions"]["done"]["status"] == "SUCCEEDED"

            # 11. Usage ledger & AI natural-language workflow draft generation
            usage = (await client.get("/api/v1/usage", headers=headers_a)).json()
            assert usage["succeeded_runs"] == 1
            assert usage["metrics"]["workflow_run"] == 1.0

            gen = (
                await client.post(
                    "/api/v1/ai/workflow-generation",
                    headers=headers_a,
                    json={
                        "project_id": boot_a["project_id"],
                        "prompt": (
                            "On webhook, fetch CRM API, triage with an AI agent, "
                            "and require human approval"
                        ),
                    },
                )
            ).json()
            assert len(gen["draft"]["graph"]["nodes"]) >= 5
            assert gen["validation"]["issues"] == []

            # 12. Temporal worker NodeActivityExecutor smoke test against DB credential resolver
            executor = NodeActivityExecutor(
                registry=create_default_registry(),
                credential_resolver=app.state.credential_resolver,
                payload_manager=PayloadManager(),
            )
            task = ActivityTask(
                run_id=uuid4(),
                organization_id=uuid4(),
                workflow_id=uuid4(),
                workflow_version_id=uuid4(),
                node_id="set1",
                node_type="core.set_data",
                worker_pool=WorkerPool.WORKFLOW,
                resolved_config_ref=PayloadRef(inline={"values": {"ok": True}}),
            )
            outcome = await executor.execute_node(task.model_dump(mode="json"))
            assert outcome["succeeded"] is True
            assert outcome["output_ref"]["inline"] == {"ok": True}
