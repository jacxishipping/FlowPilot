"""Canonical, canvas-independent workflow graph definitions."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from flowpilot_schema.enums import EdgeKind, WorkerPool

NODE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


class Position(BaseModel):
    """Optional visual canvas coordinates; ignored by execution semantics."""

    model_config = ConfigDict(extra="forbid")

    x: float = 0.0
    y: float = 0.0


class NodeUI(BaseModel):
    """Optional editor presentation metadata decoupled from execution."""

    model_config = ConfigDict(extra="allow")

    position: Position = Field(default_factory=Position)
    collapsed: bool = False
    color: str | None = None
    notes: str | None = None


class RetryPolicy(BaseModel):
    """Retry and backoff policy for transient node failures."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_interval_ms: int = Field(default=1000, ge=10, le=300_000)
    max_interval_ms: int = Field(default=60_000, ge=100, le=3_600_000)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    jitter_ratio: float = Field(default=0.2, ge=0.0, le=0.5)
    allow_retry_with_idempotency_key: bool = Field(
        default=False,
        description=(
            "Must be True AND idempotency_key_expression configured before retrying "
            "a node with SideEffectPolicy.IRREVERSIBLE."
        ),
    )


class NodePolicy(BaseModel):
    """Per-node execution constraints, permissions, and safety controls."""

    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = Field(default=30_000, ge=100, le=3_600_000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    idempotency_key_expression: str | None = None
    require_approval: bool = False
    worker_pool: WorkerPool | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    required_permissions: list[str] = Field(default_factory=list)


class LoopSpec(BaseModel):
    """Explicit bounded iteration construct; replaces arbitrary graph cycles."""

    model_config = ConfigDict(extra="forbid")

    items_expression: str = Field(
        ...,
        min_length=1,
        description="Expression resolving to a list to iterate over.",
    )
    item_variable: str = Field(default="item", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,31}$")
    index_variable: str = Field(default="index", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,31}$")
    max_iterations: int = Field(default=100, ge=1, le=10_000)
    fail_fast: bool = True
    body_graph: WorkflowGraph


class NodeDefinition(BaseModel):
    """Canonical representation of a node inside a workflow graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique stable identifier within the graph.")
    type: str = Field(
        ...,
        min_length=2,
        max_length=128,
        description="Registered node type identifier, e.g. 'core.http_request'.",
    )
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    node_version: str | None = Field(
        default=None,
        description="Optional pinned node package/type version constraint.",
    )
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, UUID] = Field(
        default_factory=dict,
        description="Mapping of credential slot name -> encrypted credential UUID.",
    )
    policy: NodePolicy = Field(default_factory=NodePolicy)
    loop: LoopSpec | None = None
    disabled: bool = False
    ui: NodeUI | None = None

    @field_validator("id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.match(value):
            raise ValueError(
                f"Invalid node id '{value}': must start with a letter and contain only "
                "letters, digits, underscores, or hyphens (max 64 chars)."
            )
        return value


class EdgeDefinition(BaseModel):
    """Directed control/data-flow edge connecting two nodes in a graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=128)
    source: str = Field(..., min_length=1, max_length=64)
    target: str = Field(..., min_length=1, max_length=64)
    kind: EdgeKind = Field(default=EdgeKind.DEFAULT)
    source_handle: str | None = None
    target_handle: str | None = None
    label: str | None = None


class WorkflowGraph(BaseModel):
    """Directed acyclic graph (DAG) of nodes and edges."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeDefinition] = Field(default_factory=list)
    edges: list[EdgeDefinition] = Field(default_factory=list)

    def node_map(self) -> dict[str, NodeDefinition]:
        return {node.id: node for node in self.nodes}


class WorkflowSettings(BaseModel):
    """Workflow-wide execution limits and defaults."""

    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = Field(default=300_000, ge=1_000, le=86_400_000)
    max_concurrent_nodes: int = Field(default=10, ge=1, le=100)
    default_retry: RetryPolicy = Field(default_factory=RetryPolicy)
    payload_inline_limit_bytes: int = Field(
        default=65_536,
        ge=1_024,
        le=1_048_576,
        description="Payloads exceeding this size are stored as external artifacts.",
    )
    error_workflow_version_id: UUID | None = Field(
        default=None,
        description="Optional pinned WorkflowVersion UUID automatically invoked on run failure.",
    )


class WorkflowDefinition(BaseModel):
    """Canonical top-level workflow definition shared across editor, API, validator, and engine."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID = Field(default_factory=uuid4)
    project_id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    revision: int = Field(default=1, ge=1)
    graph: WorkflowGraph
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    input_schema: dict[str, Any] | None = None
    output_mapping: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


LoopSpec.model_rebuild()
