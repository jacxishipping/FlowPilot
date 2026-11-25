"""First-party AI prompt node and bounded autonomous agent node with strict tool gateway."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import Field

from flowpilot import (
    ApprovalRequiredError,
    BaseNode,
    ConfigurationError,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodeResult,
    SecurityPolicyError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool


@dataclass(frozen=True)
class AgentToolCall:
    """Structured tool invocation proposed by a language model."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelCompletion:
    """Normalized LLM provider response supporting text or bounded tool calls."""

    content: str = ""
    tool_calls: tuple[AgentToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    finish_reason: str = "stop"


class ModelProvider(Protocol):
    """Provider-agnostic LLM interface injected via ExecutionContext.services."""

    async def complete(
        self,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[str] | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> ModelCompletion: ...


ToolHandler = Callable[[dict[str, Any], ExecutionContext], Awaitable[Any]]


class AiPromptConfig(NodeConfigModel):
    provider: str = Field(default="openai", min_length=1, max_length=64)
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=128)
    system_prompt: str = ""
    prompt: str = Field(..., min_length=1)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=128_000)
    max_cost_usd: float = Field(default=1.0, gt=0.0, le=100.0)
    scrub_pii: bool = False
    block_prompt_injection: bool = False
    response_schema: dict[str, Any] | None = None


class AiPromptNode(BaseNode[AiPromptConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.ai_prompt",
        name="AI Model Prompt",
        description="Invokes a configured LLM with token/cost metering and budget enforcement.",
        category="ai",
        kind=NodeKind.AI,
        worker_pool=WorkerPool.AGENTS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.LOW,
        required_permissions=("ai.invoke",),
    )
    config_model: ClassVar[type[AiPromptConfig]] = AiPromptConfig

    async def execute(self, config: AiPromptConfig, ctx: ExecutionContext) -> NodeResult:
        provider: ModelProvider | None = ctx.services.get("model_provider")
        if provider is None:
            raise ConfigurationError(
                "No 'model_provider' configured in ExecutionContext.services."
            )

        prompt_text = config.prompt
        pii_detected: list[str] = []
        if config.block_prompt_injection:
            from flowpilot_agents.guardrails import detect_prompt_injection

            inj = detect_prompt_injection(prompt_text)
            if inj.flagged:
                raise SecurityPolicyError(
                    f"Prompt injection detected (rules: {inj.matched_rules}).",
                    code="PROMPT_INJECTION_BLOCKED",
                )
        if config.scrub_pii:
            from flowpilot_agents.guardrails import scrub_pii

            scrubbed = scrub_pii(prompt_text)
            prompt_text = scrubbed.scrubbed_text
            pii_detected = scrubbed.detected_types

        messages: list[dict[str, Any]] = []
        if config.system_prompt:
            messages.append({"role": "system", "content": config.system_prompt})
        messages.append({"role": "user", "content": prompt_text})

        completion = await provider.complete(
            provider=config.provider,
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )

        if completion.cost_usd > config.max_cost_usd:
            raise SecurityPolicyError(
                f"AI call cost (${completion.cost_usd:.4f}) exceeded budget "
                f"(${config.max_cost_usd:.4f}).",
                code="AI_BUDGET_EXCEEDED",
            )

        parsed_json: dict[str, Any] | None = None
        if config.response_schema is not None:
            from flowpilot_agents.guardrails import validate_json_output_schema

            try:
                parsed_json = validate_json_output_schema(
                    completion.content,
                    config.response_schema,
                )
            except ValueError as exc:
                raise SecurityPolicyError(
                    f"AI response failed output schema enforcement: {exc}",
                    code="AI_OUTPUT_SCHEMA_VIOLATION",
                ) from exc

        ctx.record_usage("ai_tokens_input", float(completion.input_tokens), unit="tokens")
        ctx.record_usage("ai_tokens_output", float(completion.output_tokens), unit="tokens")
        ctx.record_usage("ai_cost_usd", float(completion.cost_usd), unit="usd")

        return NodeResult(
            output={
                "text": completion.content,
                "json": parsed_json,
                "pii_redacted": pii_detected,
                "model": config.model,
                "provider": config.provider,
                "usage": {
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "cost_usd": completion.cost_usd,
                },
            }
        )


class BoundedAgentConfig(NodeConfigModel):
    objective: str = Field(..., min_length=1)
    system_instructions: str = ""
    provider: str = Field(default="openai", min_length=1, max_length=64)
    model: str = Field(default="gpt-4o", min_length=1, max_length=128)
    allowed_tools: list[str] = Field(default_factory=list)
    require_approval_for_tools: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=6, ge=1, le=50)
    max_tool_calls: int = Field(default=10, ge=0, le=100)
    max_tokens: int = Field(default=8000, ge=100, le=500_000)
    max_cost_usd: float = Field(default=2.0, gt=0.0, le=100.0)


class BoundedAgentNode(BaseNode[BoundedAgentConfig, Any]):
    """Bounded autonomous AI agent governed by tool allowlists, budgets, and step caps."""

    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="core.bounded_agent",
        name="Bounded AI Agent",
        description=(
            "Runs a bounded autonomous reasoning loop with strict tool allowlists, "
            "step/token/cost budgets, and structured trace output."
        ),
        category="ai",
        kind=NodeKind.AGENT,
        worker_pool=WorkerPool.AGENTS,
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        risk_level=RiskLevel.HIGH,
        required_permissions=("ai.agent.run",),
    )
    config_model: ClassVar[type[BoundedAgentConfig]] = BoundedAgentConfig

    async def execute(self, config: BoundedAgentConfig, ctx: ExecutionContext) -> NodeResult:
        provider: ModelProvider | None = ctx.services.get("model_provider")
        if provider is None:
            raise ConfigurationError(
                "No 'model_provider' configured in ExecutionContext.services."
            )

        tool_handlers: dict[str, ToolHandler] = ctx.services.get("agent_tools", {})
        allowed_set = set(config.allowed_tools)
        approval_tools = set(config.require_approval_for_tools)

        messages: list[dict[str, Any]] = []
        if config.system_instructions:
            messages.append({"role": "system", "content": config.system_instructions})
        messages.append({"role": "user", "content": config.objective})

        trace: list[dict[str, Any]] = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0
        tool_call_count = 0

        for step_idx in range(1, config.max_steps + 1):
            if ctx.check_canceled():
                raise SecurityPolicyError("Agent execution canceled.", code="AGENT_CANCELED")

            completion = await provider.complete(
                provider=config.provider,
                model=config.model,
                messages=messages,
                tools=list(config.allowed_tools),
            )
            total_input_tokens += completion.input_tokens
            total_output_tokens += completion.output_tokens
            total_cost += completion.cost_usd

            if total_input_tokens + total_output_tokens > config.max_tokens:
                raise SecurityPolicyError(
                    f"Agent exceeded max_tokens budget ({config.max_tokens}).",
                    code="AGENT_TOKEN_BUDGET_EXCEEDED",
                )
            if total_cost > config.max_cost_usd:
                raise SecurityPolicyError(
                    f"Agent exceeded max_cost_usd budget (${config.max_cost_usd:.4f}).",
                    code="AGENT_COST_BUDGET_EXCEEDED",
                )

            ctx.record_usage("agent_steps", 1.0, unit="step")

            if not completion.tool_calls:
                trace.append({"step": step_idx, "action": "finish", "content": completion.content})
                ctx.record_usage("ai_tokens_input", float(total_input_tokens), unit="tokens")
                ctx.record_usage("ai_tokens_output", float(total_output_tokens), unit="tokens")
                ctx.record_usage("ai_cost_usd", float(total_cost), unit="usd")
                return NodeResult(
                    output={
                        "result": completion.content,
                        "steps": step_idx,
                        "tool_calls": tool_call_count,
                        "trace": trace,
                        "usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                            "cost_usd": total_cost,
                        },
                    }
                )

            for call in completion.tool_calls:
                # Tool Gateway enforcement: strict allowlist check
                if call.tool_name not in allowed_set:
                    raise SecurityPolicyError(
                        f"Tool '{call.tool_name}' is not in agent allowed_tools "
                        f"({sorted(allowed_set)}).",
                        code="AGENT_TOOL_NOT_ALLOWED",
                    )

                tool_call_count += 1
                if tool_call_count > config.max_tool_calls:
                    raise SecurityPolicyError(
                        f"Agent exceeded max_tool_calls limit ({config.max_tool_calls}).",
                        code="AGENT_TOOL_LIMIT_EXCEEDED",
                    )

                # Tool-level approval check
                if call.tool_name in approval_tools and ctx.approval_resolution is None:
                    raise ApprovalRequiredError(
                        title=f"Approve agent tool call '{call.tool_name}'",
                        description=(
                            f"Bounded agent requested approval before invoking tool "
                            f"'{call.tool_name}'."
                        ),
                        risk_level=RiskLevel.HIGH,
                        payload={"tool": call.tool_name, "arguments": call.arguments},
                    )

                handler = tool_handlers.get(call.tool_name)
                if handler is None:
                    raise ConfigurationError(
                        f"Allowed tool '{call.tool_name}' has no registered handler."
                    )

                tool_output = await handler(call.arguments, ctx)
                ctx.record_usage("tool_calls", 1.0, unit="call", metadata={"tool": call.tool_name})
                trace.append(
                    {
                        "step": step_idx,
                        "action": "tool_call",
                        "tool": call.tool_name,
                        "arguments": call.arguments,
                        "result": tool_output,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "name": call.tool_name,
                        "content": str(tool_output),
                    }
                )

        raise SecurityPolicyError(
            f"Agent reached max_steps ({config.max_steps}) without completing its objective.",
            code="AGENT_MAX_STEPS_EXCEEDED",
        )
