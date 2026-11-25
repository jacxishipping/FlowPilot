"""First-party data manipulation nodes: set_data and transform."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from flowpilot import BaseNode, ExecutionContext, NodeConfigModel, NodeMetadata, NodeResult
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class SetDataConfig(NodeConfigModel):
    values: dict[str, Any] = Field(default_factory=dict)


class SetDataNode(BaseNode[SetDataConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.set_data",
        name="Set Data",
        description="Emits a structured JSON object computed from literals and expressions.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[SetDataConfig]] = SetDataConfig

    async def execute(self, config: SetDataConfig, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(output=dict(config.values))


class TransformConfig(NodeConfigModel):
    source: dict[str, Any] = Field(default_factory=dict)
    pick: list[str] = Field(default_factory=list)
    omit: list[str] = Field(default_factory=list)
    rename: dict[str, str] = Field(default_factory=dict)
    merge: dict[str, Any] = Field(default_factory=dict)


class TransformNode(BaseNode[TransformConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.transform",
        name="Transform Object",
        description="Picks, omits, renames, and merges fields on a JSON object deterministically.",
        category="data",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
    )
    config_model: ClassVar[type[TransformConfig]] = TransformConfig

    async def execute(self, config: TransformConfig, ctx: ExecutionContext) -> NodeResult:
        data = dict(config.source)
        if config.pick:
            data = {k: data[k] for k in config.pick if k in data}
        if config.omit:
            for k in config.omit:
                data.pop(k, None)
        for old_key, new_key in config.rename.items():
            if old_key in data:
                data[new_key] = data.pop(old_key)
        data.update(config.merge)
        return NodeResult(output=data)
