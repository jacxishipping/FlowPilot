"""Sub-workflow invocation and synchronous webhook HTTP response core nodes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from uuid import UUID

from pydantic import Field

from flowpilot import (
    BaseNode,
    ConfigurationError,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    SecurityPolicyError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool

MAX_SUBWORKFLOW_DEPTH = 10

SubWorkflowRunnerFn = Callable[[UUID, UUID, dict[str, Any], int], Awaitable[dict[str, Any]]]


class SubWorkflowConfig(NodeConfigModel):
    workflow_version_id: UUID = Field(..., description="Pinned WorkflowVersion UUID to invoke.")
    input_payload: dict[str, Any] = Field(default_factory=dict)
    max_depth: int = Field(default=5, ge=1, le=MAX_SUBWORKFLOW_DEPTH)


class SubWorkflowNode(BaseNode[SubWorkflowConfig, Any]):
    """Invokes another pinned WorkflowVersion with recursion-depth and tenant guards."""

    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.subworkflow",
        name="Sub-Workflow",
        description="Calls a reusable pinned WorkflowVersion with recursion-depth enforcement.",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.MEDIUM,
    )
    config_model: ClassVar[type[SubWorkflowConfig]] = SubWorkflowConfig

    async def execute(self, config: SubWorkflowConfig, ctx: ExecutionContext) -> NodeResult:
        current_depth = int(ctx.services.get("_subworkflow_depth", 0))
        next_depth = current_depth + 1
        if next_depth > min(config.max_depth, MAX_SUBWORKFLOW_DEPTH):
            raise SecurityPolicyError(
                f"Sub-workflow recursion depth ({next_depth}) exceeded max_depth "
                f"({config.max_depth}).",
                code="SUBWORKFLOW_MAX_DEPTH_EXCEEDED",
            )

        runner: SubWorkflowRunnerFn | None = ctx.services.get("subworkflow_runner")
        if runner is None:
            raise ConfigurationError(
                "No 'subworkflow_runner' configured in ExecutionContext.services."
            )

        result = await runner(
            ctx.organization_id,
            config.workflow_version_id,
            dict(config.input_payload),
            next_depth,
        )
        ctx.log(
            "INFO",
            f"Completed sub-workflow version {config.workflow_version_id} at depth {next_depth}",
        )
        return NodeResult(output=result)


class WebhookResponseConfig(NodeConfigModel):
    status_code: int = Field(default=200, ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = Field(default_factory=dict)


class WebhookResponseNode(BaseNode[WebhookResponseConfig, Any]):
    """Returns a custom synchronous HTTP response to a webhook caller."""

    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.webhook_response",
        name="Webhook Response",
        description="Emits a custom HTTP status, headers, and JSON body for synchronous webhooks.",
        category="http",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[WebhookResponseConfig]] = WebhookResponseConfig

    async def execute(self, config: WebhookResponseConfig, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(
            output={
                "_webhook_response": True,
                "status_code": config.status_code,
                "headers": dict(config.headers),
                "body": config.body,
            }
        )
