"""First-party control-flow nodes: condition, loop, delay, and error_handler."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    BaseNode,
    EmptyConfig,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class ConditionConfig(NodeConfigModel):
    condition: Any = Field(..., description="Boolean value or resolved expression result.")


class ConditionNode(BaseNode[ConditionConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.condition",
        name="Condition (If / Else)",
        description="Routes execution to the 'true' or 'false' branch based on a condition.",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        output_branches=("true", "false"),
    )
    config_model: ClassVar[type[ConditionConfig]] = ConditionConfig

    async def execute(self, config: ConditionConfig, ctx: ExecutionContext) -> NodeResult:
        result = bool(config.condition)
        branch = "true" if result else "false"
        return NodeResult(output={"matched": result, "branch": branch}, branch=branch)


class LoopNode(BaseNode[EmptyConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.loop",
        name="Loop (For Each)",
        description="Executes a bounded sub-graph for each item in a list.",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        is_loop=True,
    )
    config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

    async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
        # Driven natively by WorkflowExecutionEngine._execute_loop_node
        return NodeResult(output={"results": [], "count": 0})


class DelayConfig(NodeConfigModel):
    duration_ms: int = Field(..., ge=0, le=86_400_000)


class DelayNode(BaseNode[DelayConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.delay",
        name="Delay / Timer",
        description="Pauses execution for a bounded duration (maps to Temporal timers).",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[DelayConfig]] = DelayConfig

    async def execute(self, config: DelayConfig, ctx: ExecutionContext) -> NodeResult:
        if config.duration_ms > 0:
            await asyncio.sleep(config.duration_ms / 1000.0)
        return NodeResult(output={"delayed_ms": config.duration_ms})


class ErrorHandlerConfig(NodeConfigModel):
    fallback_output: dict[str, Any] = Field(default_factory=dict)
    captured_error: Any = None


class ErrorHandlerNode(BaseNode[ErrorHandlerConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.error_handler",
        name="Error Handler",
        description="Catches failures on an ERROR edge and outputs structured recovery data.",
        category="control",
        kind=NodeKind.CONTROL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[ErrorHandlerConfig]] = ErrorHandlerConfig

    async def execute(self, config: ErrorHandlerConfig, ctx: ExecutionContext) -> NodeResult:
        ctx.log("WARNING", "Error branch activated", captured_error=config.captured_error)
        return NodeResult(
            output={
                "recovered": True,
                "error": config.captured_error,
                "fallback": config.fallback_output,
            }
        )
