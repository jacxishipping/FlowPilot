"""Tests for flowpilot Node SDK, SSRF protections, credential isolation, and redaction."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest

from flowpilot import (
    REDACTED_PLACEHOLDER,
    BaseNode,
    ControlledHttpClient,
    CredentialError,
    EmptyConfig,
    ExecutionContext,
    InMemoryCredentialResolver,
    NodeMetadata,
    NodeResult,
    NodeTestHarness,
    SecretValue,
    SecurityPolicyError,
    validate_outbound_url,
)
from flowpilot_nodes_core import CORE_PACKAGE_MANIFEST, create_default_registry


class _SecretEchoNode(BaseNode[EmptyConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="test.secret_echo",
        name="Secret Echo",
    )
    config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

    async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("api")
        token = cred.get_secret("token")
        ctx.log("INFO", f"Using token {token} to call service", raw_token=token)
        return NodeResult(output={"ok": True})


@pytest.mark.asyncio
async def test_credential_isolation_and_log_redaction() -> None:
    org_a = uuid4()
    org_b = uuid4()
    resolver = InMemoryCredentialResolver()
    secret_val = "super-secret-api-token-987654"
    cred_id = resolver.add(
        organization_id=org_a,
        credential_type="api_key",
        secrets={"token": secret_val},
    )

    # SecretValue never prints raw secret
    sv = SecretValue(secret_val)
    assert secret_val not in str(sv)
    assert secret_val not in repr(sv)

    # Same tenant succeeds and logs are automatically redacted
    harness_a = NodeTestHarness(organization_id=org_a, credential_resolver=resolver)
    run_res = await harness_a.run(_SecretEchoNode(), credential_bindings={"api": cred_id})
    assert run_res.result.output == {"ok": True}
    assert len(run_res.logs) == 1
    assert secret_val not in run_res.logs[0].message
    assert REDACTED_PLACEHOLDER in run_res.logs[0].message
    assert run_res.logs[0].fields["raw_token"] == REDACTED_PLACEHOLDER

    # Different tenant is blocked by tenant isolation
    harness_b = NodeTestHarness(organization_id=org_b, credential_resolver=resolver)
    with pytest.raises(CredentialError, match="not accessible"):
        await harness_b.run(_SecretEchoNode(), credential_bindings={"api": cred_id})


@pytest.mark.asyncio
async def test_http_client_ssrf_domain_redirect_and_size_protections() -> None:
    # Block localhost, loopback, cloud metadata, private RFC1918, and non-http schemes
    for blocked_url in [
        "http://127.0.0.1:8080/admin",
        "http://localhost/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/secret",
        "http://192.168.1.1/router",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ]:
        with pytest.raises(SecurityPolicyError):
            validate_outbound_url(blocked_url, resolve_dns=False)

    # Domain allowlist check
    validate_outbound_url(
        "https://api.stripe.com/v1/charges",
        allowed_domains=["stripe.com"],
        resolve_dns=False,
    )
    with pytest.raises(SecurityPolicyError, match="allowed_domains"):
        validate_outbound_url(
            "https://evil.example.com/steal",
            allowed_domains=["stripe.com"],
            resolve_dns=False,
        )

    # Redirect SSRF hop protection: 302 from public host -> cloud metadata IP must be blocked!
    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )

    client = ControlledHttpClient(transport=httpx.MockTransport(_redirect_handler))
    with pytest.raises(SecurityPolicyError):
        await client.request("GET", "https://public.example.com/redirect")

    # Max response bytes protection
    def _large_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 2048)

    small_cap_client = ControlledHttpClient(
        max_response_bytes=512,
        transport=httpx.MockTransport(_large_handler),
    )
    with pytest.raises(SecurityPolicyError, match="exceeds limit"):
        await small_cap_client.request("GET", "https://public.example.com/big")


def test_registry_catalog_and_core_manifest() -> None:
    registry = create_default_registry()
    catalog = registry.export_catalog()
    assert len(catalog) == 14
    types = {entry["metadata"]["type"] for entry in catalog}
    assert "core.manual_trigger" in types
    assert "core.http_request" in types
    assert "core.human_approval" in types
    assert "core.bounded_agent" in types
    assert "core.subworkflow" in types
    assert "core.webhook_response" in types
    assert CORE_PACKAGE_MANIFEST.first_party is True
    assert set(CORE_PACKAGE_MANIFEST.entrypoints.keys()) == types
