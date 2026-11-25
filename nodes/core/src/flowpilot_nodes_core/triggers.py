"""First-party trigger nodes: manual and webhook."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from flowpilot import BaseNode, ExecutionContext, NodeConfigModel, NodeMetadata, NodeResult
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class ManualTriggerConfig(NodeConfigModel):
    defaults: dict[str, Any] = Field(default_factory=dict)


class ManualTriggerNode(BaseNode[ManualTriggerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.manual_trigger",
        name="Manual Trigger",
        description="Starts a workflow manually with an optional JSON payload.",
        category="triggers",
        kind=NodeKind.TRIGGER,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[ManualTriggerConfig]] = ManualTriggerConfig

    async def execute(self, config: ManualTriggerConfig, ctx: ExecutionContext) -> NodeResult:
        merged = {**config.defaults, **ctx.trigger_payload}
        return NodeResult(output=merged)


class WebhookTriggerConfig(NodeConfigModel):
    path: str = Field(default="/", min_length=1, max_length=256)
    method: str = Field(default="POST", pattern=r"^(GET|POST|PUT|PATCH|DELETE)$")
    require_signature: bool = True


class WebhookTriggerNode(BaseNode[WebhookTriggerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.webhook_trigger",
        name="Webhook Trigger",
        description="Starts a workflow when an authenticated webhook request arrives.",
        category="triggers",
        kind=NodeKind.TRIGGER,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[WebhookTriggerConfig]] = WebhookTriggerConfig

    async def execute(self, config: WebhookTriggerConfig, ctx: ExecutionContext) -> NodeResult:
        payload = dict(ctx.trigger_payload)
        return NodeResult(
            output={
                "path": config.path,
                "method": config.method,
                "headers": payload.get("headers", {}),
                "query": payload.get("query", {}),
                "body": payload.get("body", payload),
            }
        )
