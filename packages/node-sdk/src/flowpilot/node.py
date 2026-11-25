"""Base node contracts, metadata descriptors, and NodeRegistry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from flowpilot.context import ExecutionContext
from flowpilot.credentials import CredentialRequirement
from flowpilot.schemas import EmptyConfig, GenericOutput, NodeResult
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class NodeMetadata(BaseModel):
    """Static metadata describing a node type for catalog, validator, and engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(..., min_length=2, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    category: str = "core"
    kind: NodeKind = NodeKind.ACTION
    version: str = "1.0.0"
    worker_pool: WorkerPool = WorkerPool.WORKFLOW
    side_effect: SideEffectPolicy = SideEffectPolicy.NONE
    risk_level: RiskLevel = RiskLevel.LOW
    credentials: tuple[CredentialRequirement, ...] = Field(default_factory=tuple)
    required_permissions: tuple[str, ...] = Field(default_factory=tuple)
    output_branches: tuple[str, ...] = Field(default_factory=tuple)
    is_loop: bool = False


class BaseNode[ConfigT: BaseModel, OutputT: BaseModel](ABC):
    """Abstract base class for all first-party and third-party FlowPilot nodes."""

    metadata: ClassVar[NodeMetadata]
    config_model: ClassVar[type[BaseModel]] = EmptyConfig
    output_model: ClassVar[type[BaseModel]] = GenericOutput

    @abstractmethod
    async def execute(
        self,
        config: ConfigT,
        ctx: ExecutionContext,
    ) -> OutputT | NodeResult | dict[str, Any]:
        """Execute the node using validated configuration and execution context."""

    @classmethod
    def config_json_schema(cls) -> dict[str, Any]:
        return cls.config_model.model_json_schema()

    @classmethod
    def output_json_schema(cls) -> dict[str, Any]:
        return cls.output_model.model_json_schema()


class NodeRegistry:
    """In-process registry mapping canonical node type strings to BaseNode implementations."""

    def __init__(self) -> None:
        self._nodes: dict[str, type[BaseNode[Any, Any]]] = {}

    def register(
        self,
        node_cls: type[BaseNode[Any, Any]],
        *,
        replace: bool = False,
    ) -> type[BaseNode[Any, Any]]:
        node_type = node_cls.metadata.type
        if node_type in self._nodes and not replace:
            raise ValueError(f"Node type '{node_type}' is already registered.")
        self._nodes[node_type] = node_cls
        return node_cls

    def get(self, node_type: str) -> type[BaseNode[Any, Any]] | None:
        return self._nodes.get(node_type)

    def require(self, node_type: str) -> type[BaseNode[Any, Any]]:
        cls = self._nodes.get(node_type)
        if cls is None:
            raise KeyError(f"Node type '{node_type}' is not registered.")
        return cls

    def list_types(self) -> list[str]:
        return sorted(self._nodes.keys())

    def export_catalog(self) -> list[dict[str, Any]]:
        """Export metadata and JSON Schemas for the editor library and AI workflow generator."""
        catalog: list[dict[str, Any]] = []
        for node_type in self.list_types():
            cls = self._nodes[node_type]
            catalog.append(
                {
                    "metadata": cls.metadata.model_dump(mode="json"),
                    "config_schema": cls.config_json_schema(),
                    "output_schema": cls.output_json_schema(),
                }
            )
        return catalog
