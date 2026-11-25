"""First-party Email integration node with irreversible side-effect protection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    BaseNode,
    CredentialRequirement,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodePackageManifest,
    NodeRegistry,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool

EmailSenderFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SendEmailConfig(NodeConfigModel):
    to: list[str] = Field(..., min_length=1, max_length=50)
    subject: str = Field(..., min_length=1, max_length=300)
    body_text: str = Field(..., min_length=1)
    from_address: str = Field(default="noreply@flowpilot.dev")
    api_endpoint: str = Field(default="https://api.resend.com/emails")


class SendEmailNode(BaseNode[SendEmailConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="email.send",
        name="Send Email",
        description="Sends a transactional email (irreversible side effect; supports idempotency).",
        category="email",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.HIGH,
        credentials=(
            CredentialRequirement(
                slot="provider",
                credential_type="email_api",
                required=True,
                description="Transactional email API key credential.",
            ),
        ),
        required_permissions=("email.send",),
    )
    config_model: ClassVar[type[SendEmailConfig]] = SendEmailConfig

    async def execute(self, config: SendEmailConfig, ctx: ExecutionContext) -> NodeResult:
        cred = await ctx.get_credential("provider")
        api_key = cred.get_secret("api_key")

        payload: dict[str, Any] = {
            "from": config.from_address,
            "to": config.to,
            "subject": config.subject,
            "text": config.body_text,
        }
        headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
        if ctx.idempotency_key:
            headers["Idempotency-Key"] = ctx.idempotency_key

        sender: EmailSenderFn | None = ctx.services.get("email_sender")
        if sender is not None:
            result = await sender({**payload, "idempotency_key": ctx.idempotency_key})
            ctx.log("INFO", f"Sent email to {len(config.to)} recipient(s)", subject=config.subject)
            return NodeResult(output={"sent": True, **result})

        resp = await ctx.http.request(
            "POST",
            config.api_endpoint,
            headers=headers,
            json_body=payload,
        )
        if resp.status_code >= 500 or resp.status_code == 429:
            raise RetryableNodeError(
                f"Email provider returned HTTP {resp.status_code}.",
                code=f"EMAIL_HTTP_{resp.status_code}",
            )
        if resp.status_code >= 400:
            raise NonRetryableNodeError(
                f"Email provider rejected request (HTTP {resp.status_code}).",
                code=f"EMAIL_HTTP_{resp.status_code}",
            )

        ctx.log("INFO", f"Sent email via HTTP provider to {len(config.to)} recipient(s)")
        return NodeResult(
            output={
                "sent": True,
                "status_code": resp.status_code,
                "response": resp.body_json if resp.body_json is not None else resp.body_text,
            }
        )


EMAIL_PACKAGE_MANIFEST = NodePackageManifest(
    package_name="flowpilot-nodes-email",
    package_version="0.1.0",
    sdk_compatibility=">=0.1.0,<1.0.0",
    first_party=True,
    entrypoints={"email.send": "flowpilot_nodes_email:SendEmailNode"},
    permissions=("email.send",),
    credential_types=("email_api",),
)


def register_email_nodes(registry: NodeRegistry, *, replace: bool = False) -> NodeRegistry:
    registry.register(SendEmailNode, replace=replace)
    return registry


__all__ = [
    "EMAIL_PACKAGE_MANIFEST",
    "EmailSenderFn",
    "SendEmailConfig",
    "SendEmailNode",
    "register_email_nodes",
]
