"""Request and response Pydantic schemas for FlowPilot /api/v1 endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema import WorkflowDefinition, WorkflowGraph, WorkflowSettings
from flowpilot_validator import ValidationResult


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    full_name: str = Field(default="Founder", max_length=200)
    password: str = Field(default="dev-password-change-me", min_length=8, max_length=128)
    organization_name: str = Field(..., min_length=1, max_length=200)
    organization_slug: str = Field(..., min_length=2, max_length=120, pattern=r"^[a-z0-9-]+$")
    workspace_name: str = Field(default="Default Workspace", max_length=200)
    project_name: str = Field(default="Default Project", max_length=200)


class BootstrapResponse(BaseModel):
    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    project_id: UUID
    api_key: str
    api_key_prefix: str


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=2, max_length=120, pattern=r"^[a-z0-9-]+$")


class OrganizationOut(BaseModel):
    id: UUID
    name: str
    slug: str
    plan: str
    created_at: datetime


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class WorkspaceOut(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    description: str
    created_at: datetime


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class ProjectOut(BaseModel):
    id: UUID
    organization_id: UUID
    workspace_id: UUID
    name: str
    description: str
    created_at: datetime


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    graph: WorkflowGraph
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    output_mapping: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(
        ...,
        ge=1,
        description="Optimistic concurrency check; returns 409 Conflict if stale.",
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    graph: WorkflowGraph | None = None
    settings: WorkflowSettings | None = None
    output_mapping: dict[str, Any] | None = None
    tags: list[str] | None = None


class WorkflowOut(BaseModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    name: str
    description: str
    revision: int
    is_active: bool
    active_version_id: UUID | None
    definition: WorkflowDefinition
    created_at: datetime
    updated_at: datetime


class PublishWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changelog: str = Field(default="", max_length=1000)
    activate: bool = True


class WorkflowVersionOut(BaseModel):
    id: UUID
    workflow_id: UUID
    version_number: int
    content_hash: str
    changelog: str
    published_at: datetime


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_input: dict[str, Any] = Field(default_factory=dict)
    trigger_type: str = Field(default="manual", max_length=64)
    workflow_version_id: UUID | None = None


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    comment: str = Field(default="", max_length=2000)


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    credential_type: str = Field(..., min_length=1, max_length=64)
    secrets: dict[str, str] = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    scopes: list[str] = Field(default_factory=list)


class CredentialOut(BaseModel):
    """Metadata-only credential view; decrypted secrets are NEVER returned by API."""

    id: UUID
    organization_id: UUID
    name: str
    credential_type: str
    key_id: str
    metadata: dict[str, Any]
    scopes: list[str]
    created_at: datetime


class WorkflowGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    prompt: str = Field(..., min_length=5, max_length=4000)
    name: str | None = Field(default=None, max_length=200)


class WorkflowGenerationResponse(BaseModel):
    draft: WorkflowDefinition
    validation: ValidationResult
    missing_configuration: list[str]
    notes: list[str]


class MarketplacePackageOut(BaseModel):
    id: UUID
    package_name: str
    package_version: str
    publisher: str
    first_party: bool
    manifest: dict[str, Any]
    published_at: datetime
