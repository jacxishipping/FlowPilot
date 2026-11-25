"""First-party HTTP integration node using the SSRF-protected ControlledHttpClient."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from flowpilot import (
    BaseNode,
    CredentialRequirement,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class HttpRequestConfig(NodeConfigModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    url: str = Field(..., min_length=8, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any | None = None
    body: str | None = None
    timeout_seconds: float = Field(default=15.0, ge=0.5, le=300.0)
    follow_redirects: bool = True
    fail_on_http_error: bool = True
    auth_header_name: str = "Authorization"
    auth_scheme: str = "Bearer"


class HttpRequestNode(BaseNode[HttpRequestConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.http_request",
        name="HTTP Request",
        description="Performs a controlled outbound HTTP request with SSRF and domain policies.",
        category="http",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IDEMPOTENT,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(
                slot="auth",
                credential_type="http_auth",
                required=False,
                description="Optional API key or Bearer token credential.",
            ),
        ),
        required_permissions=("http.outbound",),
    )
    config_model: ClassVar[type[HttpRequestConfig]] = HttpRequestConfig

    async def execute(self, config: HttpRequestConfig, ctx: ExecutionContext) -> NodeResult:
        headers = dict(config.headers)

        # Attach provider idempotency header if configured on the node policy
        if ctx.idempotency_key and "Idempotency-Key" not in headers:
            headers["Idempotency-Key"] = ctx.idempotency_key

        # Inject encrypted credential if bound
        try:
            cred = await ctx.get_credential("auth")
            token = cred.get_secret("token")
            headers[config.auth_header_name] = (
                f"{config.auth_scheme} {token}" if config.auth_scheme else token
            )
        except Exception:
            # Credential slot is optional; only fail if slot was explicitly bound
            if "auth" in ctx._credential_bindings:
                raise

        resp = await ctx.http.request(
            config.method,
            config.url,
            headers=headers,
            params=config.query_params or None,
            json_body=config.json_body,
            body=config.body,
            timeout_seconds=config.timeout_seconds,
            follow_redirects=config.follow_redirects,
        )

        ctx.log(
            "INFO",
            f"HTTP {config.method} {config.url} -> {resp.status_code}",
            status_code=resp.status_code,
        )

        if config.fail_on_http_error and resp.status_code >= 400:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableNodeError(
                    f"HTTP {resp.status_code} from {config.url}",
                    code=f"HTTP_{resp.status_code}",
                    details={"status_code": resp.status_code, "body": resp.body_text[:500]},
                )
            raise NonRetryableNodeError(
                f"HTTP {resp.status_code} from {config.url}",
                code=f"HTTP_{resp.status_code}",
                details={"status_code": resp.status_code, "body": resp.body_text[:500]},
            )

        return NodeResult(
            output={
                "status_code": resp.status_code,
                "headers": resp.headers,
                "body": resp.body_json if resp.body_json is not None else resp.body_text,
                "body_text": resp.body_text,
                "url": resp.url,
            }
        )
