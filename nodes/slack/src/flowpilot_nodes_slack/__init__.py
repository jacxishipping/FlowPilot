"""First-party Slack messaging node using encrypted bot credentials and SSRF-safe HTTP."""

from __future__ import annotations

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

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


class SlackPostMessageConfig(NodeConfigModel):
    channel: str = Field(..., min_length=1, max_length=128)
    text: str = Field(..., min_length=1, max_length=4000)
    thread_ts: str | None = None


class SlackPostMessageNode(BaseNode[SlackPostMessageConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="slack.post_message",
        name="Slack Post Message",
        description="Posts a message to a Slack channel using an encrypted bot token.",
        category="slack",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.MEDIUM,
        credentials=(
            CredentialRequirement(
                slot="bot",
                credential_type="slack_bot",
                required=True,
                scopes=("chat:write",),
                description="Slack Bot OAuth token credential.",
            ),
        ),
        required_permissions=("slack.write",),
    )
    config_model: ClassVar[type[SlackPostMessageConfig]] = SlackPostMessageConfig

    async def execute(
        self,
        config: SlackPostMessageConfig,
        ctx: ExecutionContext,
    ) -> NodeResult:
        cred = await ctx.get_credential("bot")
        token = cred.get_secret("token")

        body: dict[str, Any] = {"channel": config.channel, "text": config.text}
        if config.thread_ts:
            body["thread_ts"] = config.thread_ts

        resp = await ctx.http.request(
            "POST",
            SLACK_POST_MESSAGE_URL,
            headers={"Authorization": f"Bearer {token}"},
            json_body=body,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableNodeError(
                f"Slack API transient HTTP {resp.status_code}.",
                code=f"SLACK_HTTP_{resp.status_code}",
            )
        if resp.status_code >= 400:
            raise NonRetryableNodeError(
                f"Slack API error HTTP {resp.status_code}.",
                code=f"SLACK_HTTP_{resp.status_code}",
            )

        data = resp.body_json if isinstance(resp.body_json, dict) else {}
        if not data.get("ok", False):
            err_code = str(data.get("error", "unknown_error"))
            raise NonRetryableNodeError(
                f"Slack API returned ok=false: {err_code}",
                code=f"SLACK_API_{err_code.upper()}",
            )

        ctx.log("INFO", f"Posted Slack message to channel '{config.channel}'")
        return NodeResult(
            output={
                "ok": True,
                "channel": data.get("channel", config.channel),
                "ts": data.get("ts"),
            }
        )


SLACK_PACKAGE_MANIFEST = NodePackageManifest(
    package_name="flowpilot-nodes-slack",
    package_version="0.1.0",
    sdk_compatibility=">=0.1.0,<1.0.0",
    first_party=True,
    entrypoints={"slack.post_message": "flowpilot_nodes_slack:SlackPostMessageNode"},
    permissions=("slack.write",),
    credential_types=("slack_bot",),
)


def register_slack_nodes(registry: NodeRegistry, *, replace: bool = False) -> NodeRegistry:
    registry.register(SlackPostMessageNode, replace=replace)
    return registry


__all__ = [
    "SLACK_PACKAGE_MANIFEST",
    "SLACK_POST_MESSAGE_URL",
    "SlackPostMessageConfig",
    "SlackPostMessageNode",
    "register_slack_nodes",
]
