"""Policy-enforcing Agent Tool Gateway validating allowlists, scopes, rate limits, and approvals."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from flowpilot import ApprovalRequiredError, SecurityPolicyError
from flowpilot_agents.models import AgentDefinition, ToolSpec
from flowpilot_policy import TenantRateLimiter, has_scope
from flowpilot_schema import RiskLevel

ToolCallable = Callable[[dict[str, Any]], Awaitable[Any]]


class AgentToolGateway:
    """Mediates every tool invocation from an autonomous agent against security policies."""

    def __init__(
        self,
        *,
        rate_limiter: TenantRateLimiter | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolCallable] = {}
        self._rate_limiter = rate_limiter or TenantRateLimiter(
            max_requests=120,
            window_seconds=60.0,
        )

    def register_tool(self, spec: ToolSpec, handler: ToolCallable) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def invoke(
        self,
        *,
        agent: AgentDefinition,
        organization_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        call_counts: dict[str, int],
        approved_tools: set[str] | None = None,
    ) -> Any:
        # 1. Strict allowlist enforcement
        if tool_name not in agent.allowed_tools:
            raise SecurityPolicyError(
                f"Tool '{tool_name}' is not permitted by agent '{agent.name}' allowlist.",
                code="TOOL_NOT_ALLOWED",
            )

        spec = self._specs.get(tool_name)
        handler = self._handlers.get(tool_name)
        if spec is None or handler is None:
            raise SecurityPolicyError(
                f"Tool '{tool_name}' is not registered in the tool gateway.",
                code="TOOL_NOT_REGISTERED",
            )

        # 2. Tenant rate-limit check
        rate_key = f"{organization_id}:{tool_name}"
        if not self._rate_limiter.allow(rate_key):
            raise SecurityPolicyError(
                f"Rate limit exceeded for tool '{tool_name}' in organization '{organization_id}'.",
                code="TOOL_RATE_LIMITED",
            )

        # 3. Credential/permission scope check
        for req_scope in spec.required_scopes:
            if not has_scope(agent.credential_scopes, req_scope):
                raise SecurityPolicyError(
                    f"Agent '{agent.name}' lacks scope '{req_scope}' required by '{tool_name}'.",
                    code="TOOL_SCOPE_DENIED",
                )

        # 4. Per-tool invocation cap
        used = call_counts.get(tool_name, 0)
        if used >= spec.max_calls_per_run:
            raise SecurityPolicyError(
                f"Tool '{tool_name}' exceeded max_calls_per_run ({spec.max_calls_per_run}).",
                code="TOOL_CALL_CAP_EXCEEDED",
            )

        # 5. Risk / human approval gate
        needs_approval = (
            spec.require_approval
            or spec.risk_level == RiskLevel.CRITICAL
            or tool_name in agent.approval_required_tools
        )
        if needs_approval and tool_name not in (approved_tools or set()):
            raise ApprovalRequiredError(
                title=f"Approve agent '{agent.name}' tool '{tool_name}'",
                description=(
                    f"Tool '{tool_name}' (risk={spec.risk_level.value}) requires human approval."
                ),
                risk_level=spec.risk_level,
                payload={"agent_id": str(agent.id), "tool": tool_name, "arguments": arguments},
            )

        call_counts[tool_name] = used + 1
        return await handler(arguments)


def new_call_counter() -> dict[str, int]:
    return defaultdict(int)
