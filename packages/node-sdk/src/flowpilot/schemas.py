"""Typed Pydantic base schemas for node configurations, inputs, and outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeConfigModel(BaseModel):
    """Base class for typed node configuration models."""

    model_config = ConfigDict(extra="forbid")


class NodeOutputModel(BaseModel):
    """Base class for typed node output models."""

    model_config = ConfigDict(extra="forbid")


class EmptyConfig(NodeConfigModel):
    """Default configuration schema for nodes without configuration fields."""


class GenericOutput(BaseModel):
    """Flexible structured output model when output keys are dynamic."""

    model_config = ConfigDict(extra="allow")


class NodeResult(BaseModel):
    """Normalized return envelope produced by node execution."""

    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any] = Field(default_factory=dict)
    branch: str | None = Field(
        default=None,
        description="Optional branch selector for control nodes (e.g. 'true' or 'false').",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
