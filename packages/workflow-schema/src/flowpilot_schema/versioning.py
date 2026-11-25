"""Immutable pinned workflow version snapshots."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema.execution import utc_now
from flowpilot_schema.export import compute_definition_hash
from flowpilot_schema.graph import WorkflowDefinition


class WorkflowVersion(BaseModel):
    """Immutable published snapshot pinned by every workflow run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID
    organization_id: UUID
    version_number: int = Field(..., ge=1)
    definition: WorkflowDefinition
    content_hash: str
    published_at: datetime = Field(default_factory=utc_now)
    published_by: UUID | None = None
    changelog: str = ""

    @classmethod
    def from_definition(
        cls,
        definition: WorkflowDefinition,
        *,
        version_number: int,
        published_by: UUID | None = None,
        changelog: str = "",
    ) -> WorkflowVersion:
        snapshot = definition.model_copy(deep=True)
        return cls(
            workflow_id=snapshot.id,
            organization_id=snapshot.organization_id,
            version_number=version_number,
            definition=snapshot,
            content_hash=compute_definition_hash(snapshot, include_ui=False),
            published_by=published_by,
            changelog=changelog,
        )
