"""Execution, artifact, approval, event, and usage state models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema.enums import (
    ApprovalDecision,
    EventType,
    NodeStatus,
    RiskLevel,
    RunStatus,
    WorkerPool,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class ArtifactReference(BaseModel):
    """Pointer to an object-storage payload kept outside orchestration history."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID = Field(default_factory=uuid4)
    uri: str
    content_type: str = "application/json"
    size_bytes: int = Field(..., ge=0)
    sha256: str


class PayloadRef(BaseModel):
    """Holds either small inline JSON data or an external object-storage artifact pointer."""

    model_config = ConfigDict(extra="forbid")

    inline: Any = None
    artifact: ArtifactReference | None = None

    @property
    def is_external(self) -> bool:
        return self.artifact is not None


class ErrorDetail(BaseModel):
    """Structured error envelope for failed nodes or workflow runs."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    node_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class UsageRecord(BaseModel):
    """Immutable usage metering record for billing and quota enforcement."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    run_id: UUID
    node_id: str | None = None
    metric: str
    quantity: float = Field(..., ge=0.0)
    unit: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=utc_now)


class ApprovalRequest(BaseModel):
    """Human-in-the-loop approval gate paused inside a durable workflow run."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    run_id: UUID
    node_id: str
    title: str
    description: str = ""
    risk_level: RiskLevel = RiskLevel.HIGH
    payload: dict[str, Any] = Field(default_factory=dict)
    decision: ApprovalDecision = ApprovalDecision.PENDING
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class NodeExecution(BaseModel):
    """Per-node execution state and telemetry within a workflow run."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    node_id: str
    node_type: str
    status: NodeStatus = NodeStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    input_ref: PayloadRef | None = None
    output_ref: PayloadRef | None = None
    error: ErrorDetail | None = None
    branch_taken: str | None = None
    worker_pool: WorkerPool = WorkerPool.WORKFLOW
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None


class ExecutionEvent(BaseModel):
    """Ordered audit and real-time UI event emitted during workflow execution."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(..., ge=1)
    run_id: UUID
    organization_id: UUID
    event_type: EventType
    node_id: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowRun(BaseModel):
    """Complete durable state of a workflow execution against an immutable version."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    workflow_id: UUID
    workflow_version_id: UUID
    status: RunStatus = RunStatus.QUEUED
    trigger_type: str = "manual"
    input_ref: PayloadRef = Field(default_factory=PayloadRef)
    output_ref: PayloadRef | None = None
    error: ErrorDetail | None = None
    idempotency_key: str | None = None
    temporal_workflow_id: str | None = None
    temporal_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    node_executions: dict[str, NodeExecution] = Field(default_factory=dict)
    approvals: dict[UUID, ApprovalRequest] = Field(default_factory=dict)
    usage_records: list[UsageRecord] = Field(default_factory=list)
