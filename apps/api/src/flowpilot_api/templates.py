"""Curated ready-to-clone FlowPilot starter workflow templates."""

from __future__ import annotations

from typing import Any
from uuid import UUID

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


class TemplateInfo:
    def __init__(
        self,
        *,
        slug: str,
        name: str,
        category: str,
        description: str,
        tags: list[str],
        graph: WorkflowGraph,
    ) -> None:
        self.slug = slug
        self.name = name
        self.category = category
        self.description = description
        self.tags = tags
        self.graph = graph

    def to_summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "tags": self.tags,
            "node_count": len(self.graph.nodes),
            "graph": self.graph.model_dump(mode="json"),
        }

    def instantiate(self, *, organization_id: UUID, project_id: UUID) -> WorkflowDefinition:
        return WorkflowDefinition(
            organization_id=organization_id,
            project_id=project_id,
            name=self.name,
            description=self.description,
            graph=self.graph.model_copy(deep=True),
            tags=list(self.tags),
        )


CURATED_TEMPLATES: dict[str, TemplateInfo] = {
    "invoice-approval": TemplateInfo(
        slug="invoice-approval",
        name="Invoice Approval Pipeline",
        category="finance",
        description="Routes invoices >= $500 through a human approval gate before fulfillment.",
        tags=["finance", "approval", "starter"],
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="trigger",
                    type="core.manual_trigger",
                    name="Invoice Submitted",
                    config={"defaults": {"amount": 750, "vendor": "CloudCorp"}},
                    ui=NodeUI(position=Position(x=60, y=180)),
                ),
                NodeDefinition(
                    id="threshold_check",
                    type="core.condition",
                    name="Amount >= 500?",
                    config={"condition": "{{ nodes.trigger.output.amount >= 500 }}"},
                    ui=NodeUI(position=Position(x=340, y=180)),
                ),
                NodeDefinition(
                    id="finance_approval",
                    type="core.human_approval",
                    name="CFO Sign-Off",
                    config={
                        "title": "Approve invoice from {{ nodes.trigger.output.vendor }}",
                        "payload": {"amount": "{{ nodes.trigger.output.amount }}"},
                        "fail_on_reject": False,
                    },
                    ui=NodeUI(position=Position(x=640, y=90)),
                ),
                NodeDefinition(
                    id="auto_pay",
                    type="core.set_data",
                    name="Auto Approve",
                    config={"values": {"status": "auto_approved"}},
                    ui=NodeUI(position=Position(x=640, y=270)),
                ),
                NodeDefinition(
                    id="record_paid",
                    type="core.set_data",
                    name="Mark Approved",
                    config={
                        "values": {
                            "status": "approved",
                            "approver": "{{ nodes.finance_approval.output.decided_by }}",
                        }
                    },
                    ui=NodeUI(position=Position(x=940, y=90)),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="trigger", target="threshold_check"),
                EdgeDefinition(
                    id="e2",
                    source="threshold_check",
                    target="finance_approval",
                    kind=EdgeKind.TRUE,
                ),
                EdgeDefinition(
                    id="e3",
                    source="threshold_check",
                    target="auto_pay",
                    kind=EdgeKind.FALSE,
                ),
                EdgeDefinition(
                    id="e4",
                    source="finance_approval",
                    target="record_paid",
                    kind=EdgeKind.TRUE,
                ),
            ],
        ),
    ),
    "support-triage-agent": TemplateInfo(
        slug="support-triage-agent",
        name="Support Ticket Triage Agent",
        category="ai-agents",
        description=(
            "Receives a support webhook, classifies priority via bounded agent, and replies."
        ),
        tags=["support", "agent", "webhook"],
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="hook",
                    type="core.webhook_trigger",
                    name="Ticket Webhook",
                    ui=NodeUI(position=Position(x=60, y=180)),
                ),
                NodeDefinition(
                    id="triage",
                    type="core.bounded_agent",
                    name="Triage Agent",
                    config={
                        "objective": "Triage ticket: {{ nodes.hook.output.body | json }}",
                        "allowed_tools": [],
                        "max_steps": 4,
                        "max_cost_usd": 0.25,
                    },
                    policy=NodePolicy(retry=RetryPolicy(max_attempts=1)),
                    ui=NodeUI(position=Position(x=360, y=180)),
                ),
                NodeDefinition(
                    id="reply",
                    type="core.webhook_response",
                    name="Return Triage JSON",
                    config={
                        "status_code": 200,
                        "body": {"triage": "{{ nodes.triage.output.result }}"},
                    },
                    ui=NodeUI(position=Position(x=680, y=180)),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="hook", target="triage"),
                EdgeDefinition(id="e2", source="triage", target="reply"),
            ],
        ),
    ),
    "lead-enrichment": TemplateInfo(
        slug="lead-enrichment",
        name="Lead Enrichment & Normalization",
        category="revops",
        description="Normalizes inbound lead emails and transforms fields into CRM-ready payloads.",
        tags=["sales", "crm", "data"],
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="start",
                    type="core.manual_trigger",
                    name="Lead Inbound",
                    config={"defaults": {"email": "  VP@Acme.IO ", "company": "Acme"}},
                    ui=NodeUI(position=Position(x=60, y=180)),
                ),
                NodeDefinition(
                    id="normalize",
                    type="core.set_data",
                    name="Normalize Lead",
                    config={
                        "values": {
                            "email": "{{ nodes.start.output.email | trim | lower }}",
                            "company": "{{ nodes.start.output.company }}",
                        }
                    },
                    ui=NodeUI(position=Position(x=360, y=180)),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="normalize")],
        ),
    ),
    "incident-response": TemplateInfo(
        slug="incident-response",
        name="Incident Response Router",
        category="devops",
        description="Detects P1 severity incidents, routes escalation, and responds synchronously.",
        tags=["devops", "incident", "sre"],
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="alert",
                    type="core.webhook_trigger",
                    name="Alertmanager Webhook",
                    ui=NodeUI(position=Position(x=60, y=180)),
                ),
                NodeDefinition(
                    id="is_p1",
                    type="core.condition",
                    name="Severity == P1?",
                    config={"condition": "{{ nodes.alert.output.body.severity == 'P1' }}"},
                    ui=NodeUI(position=Position(x=340, y=180)),
                ),
                NodeDefinition(
                    id="page_oncall",
                    type="core.set_data",
                    name="Escalate P1 On-Call",
                    config={"values": {"escalated": True, "severity": "P1"}},
                    ui=NodeUI(position=Position(x=640, y=90)),
                ),
                NodeDefinition(
                    id="log_low",
                    type="core.set_data",
                    name="Log Non-Critical",
                    config={"values": {"escalated": False}},
                    ui=NodeUI(position=Position(x=640, y=270)),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="alert", target="is_p1"),
                EdgeDefinition(id="e2", source="is_p1", target="page_oncall", kind=EdgeKind.TRUE),
                EdgeDefinition(id="e3", source="is_p1", target="log_low", kind=EdgeKind.FALSE),
            ],
        ),
    ),
    "daily-digest": TemplateInfo(
        slug="daily-digest",
        name="Daily Executive AI Digest",
        category="analytics",
        description="Summarizes daily operational metrics with PII scrubbing enabled.",
        tags=["ai", "digest", "pii-safe"],
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(
                    id="schedule",
                    type="core.manual_trigger",
                    name="Daily Trigger",
                    config={"defaults": {"metrics": {"runs": 420, "errors": 2}}},
                    ui=NodeUI(position=Position(x=60, y=180)),
                ),
                NodeDefinition(
                    id="summarize",
                    type="core.ai_prompt",
                    name="AI Digest (PII-Safe)",
                    config={
                        "prompt": "Summarize metrics: {{ nodes.schedule.output.metrics | json }}",
                        "scrub_pii": True,
                        "block_prompt_injection": True,
                    },
                    ui=NodeUI(position=Position(x=360, y=180)),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="schedule", target="summarize")],
        ),
    ),
}
