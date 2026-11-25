"""Natural-language workflow draft generator producing validated canonical WorkflowDefinitions.

Follows Section 7 strictly: generates typed canonical graph JSON (never executable code)
and NEVER auto-publishes or auto-executes generated drafts.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flowpilot import NodeRegistry
from flowpilot_schema import (
    EdgeDefinition,
    EdgeKind,
    NodeDefinition,
    NodePolicy,
    NodeUI,
    Position,
    RetryPolicy,
    WorkflowDefinition,
    WorkflowGraph,
)
from flowpilot_validator import WorkflowValidator


def generate_workflow_draft(
    *,
    prompt: str,
    organization_id: UUID,
    project_id: UUID,
    registry: NodeRegistry,
    name: str | None = None,
) -> tuple[WorkflowDefinition, list[str], list[str]]:
    """Translate a natural-language automation prompt into an editable canonical graph draft."""
    lower = prompt.lower()
    nodes: list[NodeDefinition] = []
    edges: list[EdgeDefinition] = []
    missing_config: list[str] = []
    notes: list[str] = [
        "Generated as an editable draft only; review, test, and publish explicitly."
    ]

    x_cursor = 80.0
    y_base = 180.0
    step_x = 280.0

    # 1. Trigger selection
    if "webhook" in lower or "http event" in lower or "incoming" in lower:
        trigger_node = NodeDefinition(
            id="trigger",
            type="core.webhook_trigger",
            name="Webhook Trigger",
            config={"path": "/hooks/incoming", "method": "POST", "require_signature": True},
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
    else:
        trigger_node = NodeDefinition(
            id="trigger",
            type="core.manual_trigger",
            name="Manual Trigger",
            config={"defaults": {}},
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
    nodes.append(trigger_node)
    prev_id = trigger_node.id
    x_cursor += step_x

    # 2. Optional HTTP fetch / integration step
    if any(k in lower for k in ("http", "api", "fetch", "crm", "lookup", "stripe", "slack")):
        http_node = NodeDefinition(
            id="fetch_api",
            type="core.http_request",
            name="Fetch External API",
            config={
                "method": "GET",
                "url": "https://api.example.com/v1/resource",
                "fail_on_http_error": True,
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
        nodes.append(http_node)
        edges.append(
            EdgeDefinition(
                id=f"e_{prev_id}_{http_node.id}",
                source=prev_id,
                target=http_node.id,
            )
        )
        missing_config.append(
            "Node 'fetch_api': replace placeholder URL and attach an encrypted 'auth' credential."
        )
        prev_id = http_node.id
        x_cursor += step_x

    # 3. Optional AI prompt or bounded agent step
    if "agent" in lower or "autonomous" in lower or "triage" in lower:
        agent_node = NodeDefinition(
            id="ai_agent",
            type="core.bounded_agent",
            name="Bounded AI Agent",
            config={
                "objective": f"Accomplish task from trigger input: {prompt[:160]}",
                "allowed_tools": [],
                "max_steps": 5,
                "max_tool_calls": 5,
                "max_tokens": 4000,
                "max_cost_usd": 0.50,
            },
            policy=NodePolicy(retry=RetryPolicy(max_attempts=1)),
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
        nodes.append(agent_node)
        edges.append(
            EdgeDefinition(
                id=f"e_{prev_id}_{agent_node.id}",
                source=prev_id,
                target=agent_node.id,
            )
        )
        missing_config.append(
            "Node 'ai_agent': configure 'allowed_tools' allowlist and verify token/cost budgets."
        )
        prev_id = agent_node.id
        x_cursor += step_x
    elif any(k in lower for k in ("ai", "llm", "summarize", "classify", "draft", "extract")):
        ai_prompt_text = (
            f"Process input for request: {prompt[:160]} — "
            f"{{{{ nodes.{prev_id}.output | json }}}}"
        )
        ai_node = NodeDefinition(
            id="ai_step",
            type="core.ai_prompt",
            name="AI Processing",
            config={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "prompt": ai_prompt_text,
                "max_tokens": 1024,
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
        nodes.append(ai_node)
        edges.append(
            EdgeDefinition(
                id=f"e_{prev_id}_{ai_node.id}",
                source=prev_id,
                target=ai_node.id,
            )
        )
        prev_id = ai_node.id
        x_cursor += step_x

    # 4. Optional human approval gate
    if any(k in lower for k in ("approve", "approval", "review", "human", "confirm", "payment")):
        approval_node = NodeDefinition(
            id="human_approval",
            type="core.human_approval",
            name="Human Approval Gate",
            config={
                "title": "Review and approve automated action",
                "description": prompt[:300],
                "payload": {"upstream": f"{{{{ nodes.{prev_id}.output }}}}"},
                "fail_on_reject": False,
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
        nodes.append(approval_node)
        edges.append(
            EdgeDefinition(
                id=f"e_{prev_id}_{approval_node.id}",
                source=prev_id,
                target=approval_node.id,
            )
        )
        x_cursor += step_x

        approved_node = NodeDefinition(
            id="approved_action",
            type="core.set_data",
            name="Approved Outcome",
            config={
                "values": {
                    "status": "approved",
                    "approved_by": "{{ nodes.human_approval.output.decided_by }}",
                }
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base - 90)),
        )
        rejected_node = NodeDefinition(
            id="rejected_action",
            type="core.set_data",
            name="Rejected Outcome",
            config={
                "values": {
                    "status": "rejected",
                    "reason": "{{ nodes.human_approval.output.comment }}",
                }
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base + 110)),
        )
        nodes.extend([approved_node, rejected_node])
        edges.extend(
            [
                EdgeDefinition(
                    id="e_approve_true",
                    source=approval_node.id,
                    target=approved_node.id,
                    kind=EdgeKind.TRUE,
                    label="Approved",
                ),
                EdgeDefinition(
                    id="e_approve_false",
                    source=approval_node.id,
                    target=rejected_node.id,
                    kind=EdgeKind.FALSE,
                    label="Rejected",
                ),
            ]
        )
    else:
        final_node = NodeDefinition(
            id="format_output",
            type="core.set_data",
            name="Format Output",
            config={
                "values": {
                    "summary": "Workflow completed",
                    "data": f"{{{{ nodes.{prev_id}.output }}}}",
                }
            },
            ui=NodeUI(position=Position(x=x_cursor, y=y_base)),
        )
        nodes.append(final_node)
        edges.append(
            EdgeDefinition(
                id=f"e_{prev_id}_{final_node.id}",
                source=prev_id,
                target=final_node.id,
            )
        )

    wf_name = name or f"Draft: {prompt.strip()[:48]}"
    draft = WorkflowDefinition(
        organization_id=organization_id,
        project_id=project_id,
        name=wf_name,
        description=f"Generated from natural-language prompt: {prompt}",
        graph=WorkflowGraph(nodes=nodes, edges=edges),
    )

    # Validate immediately so UI receives accurate diagnostics
    validator = WorkflowValidator(registry)
    validation = validator.validate(draft)
    if not validation.is_valid:
        notes.append("Draft has validation issues that must be resolved before publishing.")

    return draft, missing_config, notes


async def generate_workflow_draft_async(
    *,
    prompt: str,
    organization_id: UUID,
    project_id: UUID,
    registry: NodeRegistry,
    name: str | None = None,
    model_provider: Any | None = None,
) -> tuple[WorkflowDefinition, list[str], list[str]]:
    """LLM-assisted canonical workflow draft generator with deterministic typed fallback."""
    import json

    if model_provider is not None and hasattr(model_provider, "complete"):
        catalog_types = registry.list_types()
        sys_prompt = (
            "You generate strictly valid FlowPilot canonical WorkflowGraph JSON objects with "
            f"keys 'nodes' and 'edges'. Allowed node types: {catalog_types}. "
            "Respond ONLY with raw JSON."
        )
        try:
            completion = await model_provider.complete(
                provider="openai",
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
            parsed = json.loads(completion.content)
            graph = WorkflowGraph.model_validate(parsed)
            draft = WorkflowDefinition(
                organization_id=organization_id,
                project_id=project_id,
                name=name or f"AI Draft: {prompt.strip()[:44]}",
                description=f"LLM-generated draft for: {prompt}",
                graph=graph,
            )
            return (
                draft,
                [],
                [
                    "Generated via configured LLM provider as an editable draft "
                    "(never auto-published)."
                ],
            )
        except Exception:
            # Fall back safely to deterministic typed generator if LLM output is invalid JSON
            pass

    return generate_workflow_draft(
        prompt=prompt,
        organization_id=organization_id,
        project_id=project_id,
        registry=registry,
        name=name,
    )
