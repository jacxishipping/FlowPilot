"""Bounded single-agent runtime and supervisor-based multi-agent team orchestrator."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from flowpilot import ApprovalRequiredError, SecurityPolicyError
from flowpilot_agents.gateway import AgentToolGateway, new_call_counter
from flowpilot_agents.memory import AgentMemoryStore
from flowpilot_agents.models import (
    AgentDefinition,
    AgentRunResult,
    DelegationContract,
    EscalationBehavior,
    TraceEntry,
)
from flowpilot_nodes_core import ModelProvider


class AgentRuntime:
    """Executes a bounded AgentDefinition through the 7-step governed agent cycle."""

    def __init__(
        self,
        *,
        model_provider: ModelProvider,
        tool_gateway: AgentToolGateway | None = None,
        memory_store: AgentMemoryStore | None = None,
    ) -> None:
        self._provider = model_provider
        self._gateway = tool_gateway or AgentToolGateway()
        self._memory = memory_store

    async def run(
        self,
        agent: AgentDefinition,
        *,
        organization_id: UUID,
        task_input: str | None = None,
        approved_tools: set[str] | None = None,
    ) -> AgentRunResult:
        if agent.organization_id != organization_id:
            raise SecurityPolicyError(
                f"Tenant mismatch: agent '{agent.id}' does not belong to org '{organization_id}'.",
                code="AGENT_TENANT_MISMATCH",
            )

        messages: list[dict[str, Any]] = []
        if agent.system_instructions:
            messages.append({"role": "system", "content": agent.system_instructions})

        objective = task_input or agent.objective
        if agent.memory_policy.enabled and self._memory is not None:
            recalled = await self._memory.recall(
                organization_id=organization_id,
                agent_id=agent.id,
                query=objective,
                limit=agent.memory_policy.max_recall_items,
            )
            if recalled:
                mem_block = "\n".join(f"- {r.content}" for r in recalled)
                messages.append(
                    {"role": "system", "content": f"Relevant tenant memory:\n{mem_block}"}
                )

        messages.append({"role": "user", "content": objective})

        trace: list[TraceEntry] = []
        call_counts = new_call_counter()
        total_in = 0
        total_out = 0
        total_cost = 0.0
        total_tool_calls = 0
        deadline = time.monotonic() + (agent.budget.max_duration_ms / 1000.0)

        for step in range(1, agent.budget.max_steps + 1):
            if time.monotonic() > deadline:
                return self._handle_limit(
                    agent,
                    reason=f"Exceeded max_duration_ms ({agent.budget.max_duration_ms}ms).",
                    steps=step - 1,
                    tool_calls=total_tool_calls,
                    in_tok=total_in,
                    out_tok=total_out,
                    cost=total_cost,
                    trace=trace,
                )

            completion = await self._provider.complete(
                provider=agent.provider,
                model=agent.model,
                messages=messages,
                tools=list(agent.allowed_tools),
            )
            total_in += completion.input_tokens
            total_out += completion.output_tokens
            total_cost += completion.cost_usd

            if total_in + total_out > agent.budget.max_tokens:
                return self._handle_limit(
                    agent,
                    reason=f"Exceeded max_tokens ({agent.budget.max_tokens}).",
                    steps=step,
                    tool_calls=total_tool_calls,
                    in_tok=total_in,
                    out_tok=total_out,
                    cost=total_cost,
                    trace=trace,
                )

            if total_cost > agent.budget.max_cost_usd:
                return self._handle_limit(
                    agent,
                    reason=f"Exceeded max_cost_usd (${agent.budget.max_cost_usd:.4f}).",
                    steps=step,
                    tool_calls=total_tool_calls,
                    in_tok=total_in,
                    out_tok=total_out,
                    cost=total_cost,
                    trace=trace,
                )

            if not completion.tool_calls:
                trace.append(
                    TraceEntry(
                        step=step,
                        agent_name=agent.name,
                        action="finish",
                        result=completion.content,
                    )
                )
                if (
                    agent.memory_policy.enabled
                    and agent.memory_policy.write_on_complete
                    and self._memory is not None
                    and completion.content
                ):
                    await self._memory.remember(
                        organization_id=organization_id,
                        agent_id=agent.id,
                        content=completion.content,
                    )
                return AgentRunResult(
                    status="completed",
                    output=completion.content,
                    steps=step,
                    tool_calls=total_tool_calls,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    cost_usd=total_cost,
                    trace=trace,
                )

            for call in completion.tool_calls:
                total_tool_calls += 1
                if total_tool_calls > agent.budget.max_tool_calls:
                    return self._handle_limit(
                        agent,
                        reason=f"Exceeded max_tool_calls ({agent.budget.max_tool_calls}).",
                        steps=step,
                        tool_calls=total_tool_calls - 1,
                        in_tok=total_in,
                        out_tok=total_out,
                        cost=total_cost,
                        trace=trace,
                    )

                try:
                    tool_result = await self._gateway.invoke(
                        agent=agent,
                        organization_id=organization_id,
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        call_counts=call_counts,
                        approved_tools=approved_tools,
                    )
                except ApprovalRequiredError as sig:
                    trace.append(
                        TraceEntry(
                            step=step,
                            agent_name=agent.name,
                            action="approval_requested",
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                        )
                    )
                    return AgentRunResult(
                        status="waiting_approval",
                        steps=step,
                        tool_calls=total_tool_calls - 1,
                        input_tokens=total_in,
                        output_tokens=total_out,
                        cost_usd=total_cost,
                        trace=trace,
                        pending_approval={
                            "title": sig.title,
                            "description": sig.description,
                            "risk_level": sig.risk_level.value,
                            "payload": sig.payload,
                        },
                    )

                trace.append(
                    TraceEntry(
                        step=step,
                        agent_name=agent.name,
                        action="tool_call",
                        tool_name=call.tool_name,
                        arguments=call.arguments,
                        result=tool_result,
                    )
                )
                messages.append(
                    {"role": "tool", "name": call.tool_name, "content": str(tool_result)}
                )

        return self._handle_limit(
            agent,
            reason=f"Reached max_steps ({agent.budget.max_steps}) without completing objective.",
            steps=agent.budget.max_steps,
            tool_calls=total_tool_calls,
            in_tok=total_in,
            out_tok=total_out,
            cost=total_cost,
            trace=trace,
        )

    @staticmethod
    def _handle_limit(
        agent: AgentDefinition,
        *,
        reason: str,
        steps: int,
        tool_calls: int,
        in_tok: int,
        out_tok: int,
        cost: float,
        trace: list[TraceEntry],
    ) -> AgentRunResult:
        if agent.escalation_behavior == EscalationBehavior.ESCALATE_HUMAN:
            status_val = "escalated"
        elif agent.escalation_behavior == EscalationBehavior.RETURN_PARTIAL:
            status_val = "completed"
        else:
            status_val = "failed"
        return AgentRunResult(
            status=status_val,
            output=f"[{status_val}] {reason}",
            steps=steps,
            tool_calls=tool_calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            trace=trace,
            error=None if status_val == "completed" else reason,
        )


class SupervisorTeamRuntime:
    """Supervisor-based multi-agent team orchestrator with explicit DelegationContracts.

    Avoids unconstrained agent-to-agent conversations: only the supervisor can delegate
    bounded sub-tasks to registered specialists, with strict budget and tool intersection.
    """

    def __init__(
        self,
        *,
        supervisor: AgentDefinition,
        specialists: list[AgentDefinition],
        runtime: AgentRuntime,
        max_team_cost_usd: float = 5.0,
    ) -> None:
        self._supervisor = supervisor
        self._specialists: dict[UUID, AgentDefinition] = {s.id: s for s in specialists}
        self._runtime = runtime
        self._max_team_cost_usd = max_team_cost_usd

    async def execute_delegations(
        self,
        *,
        organization_id: UUID,
        contracts: list[DelegationContract],
        approved_tools: set[str] | None = None,
    ) -> AgentRunResult:
        """Execute a list of explicit supervisor DelegationContracts and synthesize final output."""
        if self._supervisor.organization_id != organization_id:
            raise SecurityPolicyError(
                "Supervisor tenant organization mismatch.",
                code="SUPERVISOR_TENANT_MISMATCH",
            )

        combined_trace: list[TraceEntry] = []
        delegation_outputs: dict[str, Any] = {}
        total_steps = 0
        total_tool_calls = 0
        total_in = 0
        total_out = 0
        total_cost = 0.0

        for idx, contract in enumerate(contracts, start=1):
            specialist = self._specialists.get(contract.specialist_id)
            if specialist is None:
                raise SecurityPolicyError(
                    f"Specialist agent '{contract.specialist_id}' is not part of this team.",
                    code="UNKNOWN_DELEGATION_TARGET",
                )
            if specialist.organization_id != organization_id:
                raise SecurityPolicyError(
                    f"Specialist '{specialist.id}' belongs to another tenant.",
                    code="SPECIALIST_TENANT_MISMATCH",
                )

            # Restrict specialist tools & budget to the intersection with the delegation contract
            effective_tools = list(specialist.allowed_tools)
            if contract.allowed_tools_subset is not None:
                subset = set(contract.allowed_tools_subset)
                effective_tools = [t for t in effective_tools if t in subset]

            remaining_team_cost = max(0.0001, self._max_team_cost_usd - total_cost)
            bounded_specialist = specialist.model_copy(
                deep=True,
                update={
                    "allowed_tools": effective_tools,
                    "budget": specialist.budget.model_copy(
                        update={
                            "max_steps": min(specialist.budget.max_steps, contract.max_steps),
                            "max_cost_usd": min(
                                specialist.budget.max_cost_usd,
                                contract.max_cost_usd,
                                remaining_team_cost,
                            ),
                        }
                    ),
                },
            )

            combined_trace.append(
                TraceEntry(
                    step=idx,
                    agent_name=self._supervisor.name,
                    action="delegate",
                    tool_name=specialist.name,
                    arguments={"task": contract.task, "allowed_tools": effective_tools},
                )
            )

            sub_res = await self._runtime.run(
                bounded_specialist,
                organization_id=organization_id,
                task_input=contract.task,
                approved_tools=approved_tools,
            )
            combined_trace.extend(sub_res.trace)
            total_steps += sub_res.steps
            total_tool_calls += sub_res.tool_calls
            total_in += sub_res.input_tokens
            total_out += sub_res.output_tokens
            total_cost += sub_res.cost_usd
            delegation_outputs[specialist.name] = sub_res.output

            if sub_res.status != "completed":
                return AgentRunResult(
                    status=sub_res.status,
                    output=sub_res.output,
                    structured_output={"delegations": delegation_outputs},
                    steps=total_steps,
                    tool_calls=total_tool_calls,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    cost_usd=total_cost,
                    trace=combined_trace,
                    pending_approval=sub_res.pending_approval,
                    error=sub_res.error,
                )

            if total_cost > self._max_team_cost_usd:
                return AgentRunResult(
                    status="failed",
                    structured_output={"delegations": delegation_outputs},
                    steps=total_steps,
                    tool_calls=total_tool_calls,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    cost_usd=total_cost,
                    trace=combined_trace,
                    error=f"Team cost budget exceeded (${self._max_team_cost_usd:.4f}).",
                )

        # Final supervisor synthesis step
        summary_prompt = (
            f"Synthesize specialist outputs for objective '{self._supervisor.objective}': "
            f"{delegation_outputs}"
        )
        final_res = await self._runtime.run(
            self._supervisor,
            organization_id=organization_id,
            task_input=summary_prompt,
            approved_tools=approved_tools,
        )
        combined_trace.extend(final_res.trace)
        return AgentRunResult(
            status=final_res.status,
            output=final_res.output,
            structured_output={"delegations": delegation_outputs},
            steps=total_steps + final_res.steps,
            tool_calls=total_tool_calls + final_res.tool_calls,
            input_tokens=total_in + final_res.input_tokens,
            output_tokens=total_out + final_res.output_tokens,
            cost_usd=total_cost + final_res.cost_usd,
            trace=combined_trace,
            error=final_res.error,
        )
