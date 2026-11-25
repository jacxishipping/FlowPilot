"""Typed agent definitions, budgets, tool policies, delegation contracts, and trace models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema import RiskLevel


class EscalationBehavior(StrEnum):
    FAIL = "fail"
    ESCALATE_HUMAN = "escalate_human"
    RETURN_PARTIAL = "return_partial"


class MemoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_recall_items: int = Field(default=5, ge=1, le=50)
    write_on_complete: bool = False


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=8, ge=1, le=50)
    max_duration_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    max_tool_calls: int = Field(default=12, ge=0, le=100)
    max_tokens: int = Field(default=16_000, ge=100, le=1_000_000)
    max_cost_usd: float = Field(default=2.0, gt=0.0, le=250.0)


class ToolSpec(BaseModel):
    """Governance policy declared per callable tool in the Tool Gateway."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    require_approval: bool = False
    max_calls_per_run: int = Field(default=10, ge=1, le=100)
    required_scopes: tuple[str, ...] = Field(default_factory=tuple)


class AgentDefinition(BaseModel):
    """Canonical bounded AI agent definition (Section 6)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=200)
    objective: str = Field(..., min_length=1)
    system_instructions: str = ""
    provider: str = Field(default="openai", min_length=1, max_length=64)
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=128)
    allowed_tools: list[str] = Field(default_factory=list)
    credential_scopes: list[str] = Field(default_factory=list)
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    completion_conditions: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    escalation_behavior: EscalationBehavior = EscalationBehavior.FAIL


class DelegationContract(BaseModel):
    """Explicit bounded delegation contract from a supervisor to a specialist agent."""

    model_config = ConfigDict(extra="forbid")

    specialist_id: UUID
    task: str = Field(..., min_length=1)
    allowed_tools_subset: list[str] | None = None
    max_steps: int = Field(default=4, ge=1, le=20)
    max_cost_usd: float = Field(default=0.50, gt=0.0, le=50.0)
    expected_output_keys: list[str] = Field(default_factory=list)


class TraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    agent_name: str
    action: str
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str  # 'completed' | 'waiting_approval' | 'escalated' | 'failed'
    output: str = ""
    structured_output: dict[str, Any] = Field(default_factory=dict)
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    trace: list[TraceEntry] = Field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    error: str | None = None
