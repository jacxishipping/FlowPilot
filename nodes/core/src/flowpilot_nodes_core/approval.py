"""First-party human-in-the-loop approval gate node."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    ApprovalRequiredError,
    BaseNode,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    NonRetryableNodeError,
)
from flowpilot_schema import ApprovalDecision, NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


class HumanApprovalConfig(NodeConfigModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    risk_level: RiskLevel = RiskLevel.HIGH
    payload: dict[str, Any] = Field(default_factory=dict)
    fail_on_reject: bool = Field(
        default=False,
        description=(
            "If False, routes rejected approvals down the 'false' branch. "
            "If True, fails the node with APPROVAL_REJECTED."
        ),
    )


class HumanApprovalNode(BaseNode[HumanApprovalConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.human_approval",
        name="Human Approval Gate",
        description="Pauses the workflow run until an authorized human approves or rejects.",
        category="control",
        kind=NodeKind.APPROVAL,
        worker_pool=WorkerPool.WORKFLOW,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.HIGH,
        output_branches=("true", "false"),
    )
    config_model: ClassVar[type[HumanApprovalConfig]] = HumanApprovalConfig

    async def execute(self, config: HumanApprovalConfig, ctx: ExecutionContext) -> NodeResult:
        resolution = ctx.approval_resolution
        if resolution is None or resolution.decision == ApprovalDecision.PENDING:
            raise ApprovalRequiredError(
                title=config.title,
                description=config.description,
                risk_level=config.risk_level,
                payload=config.payload,
            )

        approved = resolution.decision == ApprovalDecision.APPROVED
        if not approved and config.fail_on_reject:
            raise NonRetryableNodeError(
                resolution.comment or f"Approval '{config.title}' was rejected.",
                code="APPROVAL_REJECTED",
                details={
                    "approval_id": str(resolution.id),
                    "decided_by": resolution.decided_by,
                },
            )

        return NodeResult(
            output={
                "approval_id": str(resolution.id),
                "approved": approved,
                "decision": resolution.decision.value,
                "decided_by": resolution.decided_by,
                "comment": resolution.comment or "",
            },
            branch="true" if approved else "false",
        )
