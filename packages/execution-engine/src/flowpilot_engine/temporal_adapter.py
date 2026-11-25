"""Temporal-compatible orchestration port and deterministic activity envelopes.

Allows the same GraphInterpreter and Node SDK contracts to execute either in-process
(for unit tests / local dev) or across distributed Temporal workflow & activity workers.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema import ErrorDetail, PayloadRef, WorkerPool


class ActivityTask(BaseModel):
    """Deterministic Temporal activity payload dispatched to a specialized worker pool.

    Carries only identifiers, pinned version info, and PayloadRef pointers (never raw secrets
    or oversized payloads) so Temporal workflow history stays compact and secure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    organization_id: UUID
    workflow_id: UUID
    workflow_version_id: UUID
    node_id: str
    node_type: str
    worker_pool: WorkerPool
    attempt: int = Field(default=1, ge=1)
    idempotency_key: str | None = None
    resolved_config_ref: PayloadRef
    credential_bindings: dict[str, UUID] = Field(default_factory=dict)
    timeout_ms: int = Field(default=30_000, ge=100)


class ActivityOutcome(BaseModel):
    """Compact deterministic result returned from a Temporal worker activity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    succeeded: bool
    output_ref: PayloadRef | None = None
    branch_taken: str | None = None
    error: ErrorDetail | None = None
    approval_required: dict[str, Any] | None = None
    duration_ms: int = Field(default=0, ge=0)


class ApprovalSignal(BaseModel):
    """Temporal workflow signal payload resuming a WAITING_APPROVAL run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: UUID
    approved: bool
    decided_by: str
    comment: str = ""


class TemporalOrchestrationPort(Protocol):
    """Port implemented by a Temporal client/worker adapter to dispatch activities."""

    async def start_workflow(
        self,
        *,
        run_id: UUID,
        organization_id: UUID,
        workflow_id: UUID,
        workflow_version_id: UUID,
        task_queue: str,
    ) -> tuple[str, str]:
        """Start a durable Temporal workflow execution and return (workflow_id, run_id)."""
        ...

    async def dispatch_activity(self, task: ActivityTask) -> ActivityOutcome:
        """Dispatch a node activity to the task queue corresponding to task.worker_pool."""
        ...

    async def send_approval_signal(self, temporal_workflow_id: str, signal: ApprovalSignal) -> None:
        """Deliver a human approval decision signal to a paused Temporal workflow."""
        ...

    async def cancel_workflow(self, temporal_workflow_id: str) -> None:
        """Send cancellation request to a running Temporal workflow."""
        ...


def task_queue_for_pool(pool: WorkerPool) -> str:
    """Canonical Temporal task queue name for each isolated worker pool."""
    return f"flowpilot-{pool.value}-workers"
