"""Tests for policy-engine, supervisor multi-agent teams, integration nodes, webhooks,
Stripe billing idempotency, cron scheduler, and OpenTelemetry observability."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from flowpilot import InMemoryCredentialResolver, NodeTestHarness, SecurityPolicyError
from flowpilot_agents import (
    AgentBudget,
    AgentDefinition,
    AgentRuntime,
    AgentToolGateway,
    DelegationContract,
    EscalationBehavior,
    InMemoryVectorMemoryStore,
    MemoryPolicy,
    SupervisorTeamRuntime,
    ToolSpec,
)
from flowpilot_api import Settings, create_app
from flowpilot_api.db.models import WorkflowTriggerModel
from flowpilot_api.webhooks import compute_webhook_signature
from flowpilot_nodes_core import AgentToolCall, ModelCompletion
from flowpilot_nodes_database import DatabaseQueryNode
from flowpilot_nodes_email import SendEmailNode
from flowpilot_nodes_slack import SlackPostMessageNode
from flowpilot_observability import (
    MetricsRegistry,
    format_structured_log,
    trace_node_execution,
    trace_workflow_run,
)
from flowpilot_policy import (
    PolicyDeniedError,
    Role,
    TenantRateLimiter,
    authorize,
    evaluate_quota,
)
from flowpilot_scheduler import TriggerScheduler, cron_matches
from flowpilot_schema import RiskLevel


def test_rbac_quotas_and_rate_limiter() -> None:
    authorize(role=Role.ADMIN, scopes=["workflows:*"], required_permission="workflows:publish")

    with pytest.raises(PolicyDeniedError):
        authorize(role=Role.VIEWER, required_permission="workflows:publish")

    with pytest.raises(PolicyDeniedError):
        authorize(role=Role.ADMIN, scopes=["runs:read"], required_permission="workflows:publish")

    # Free tier blocks overage; Pro tier warns at 80% and permits overage
    free_over = evaluate_quota(plan_name="free", metric="workflow_run", current_usage=500)
    assert not free_over.allowed

    pro_warn = evaluate_quota(plan_name="pro", metric="workflow_run", current_usage=20_500)
    assert pro_warn.allowed
    assert pro_warn.warning is not None and "82%" in pro_warn.warning

    pro_over = evaluate_quota(plan_name="pro", metric="workflow_run", current_usage=25_000)
    assert pro_over.allowed
    assert pro_over.in_overage

    limiter = TenantRateLimiter(max_requests=2, window_seconds=60.0)
    assert limiter.allow("org1", now=100.0) is True
    assert limiter.allow("org1", now=110.0) is True
    assert limiter.allow("org1", now=120.0) is False
    assert limiter.allow("org1", now=161.0) is True


@pytest.mark.asyncio
async def test_supervisor_multi_agent_team_tool_gateway_and_vector_memory() -> None:
    org_id = uuid4()

    class _ScriptedProvider:
        def __init__(self, responses: list[ModelCompletion]) -> None:
            self._responses = list(responses)
            self._idx = 0

        async def complete(self, **kwargs: Any) -> ModelCompletion:
            res = self._responses[self._idx]
            self._idx += 1
            return res

    gateway = AgentToolGateway()

    async def _search_kb(args: dict[str, Any]) -> dict[str, Any]:
        return {"doc": f"Refund policy for {args['topic']}: 30 days"}

    async def _issue_refund(args: dict[str, Any]) -> dict[str, Any]:
        return {"refunded": args["amount"]}

    gateway.register_tool(
        ToolSpec(name="search_kb", risk_level=RiskLevel.LOW, required_scopes=("kb:read",)),
        _search_kb,
    )
    gateway.register_tool(
        ToolSpec(
            name="issue_refund",
            risk_level=RiskLevel.HIGH,
            require_approval=True,
            required_scopes=("billing:refund",),
        ),
        _issue_refund,
    )

    memory = InMemoryVectorMemoryStore()
    researcher = AgentDefinition(
        organization_id=org_id,
        name="Researcher",
        objective="Research refund policies",
        allowed_tools=["search_kb"],
        credential_scopes=["kb:read"],
        memory_policy=MemoryPolicy(enabled=True, write_on_complete=True),
        budget=AgentBudget(max_steps=4, max_cost_usd=0.5),
    )
    await memory.remember(
        organization_id=org_id,
        agent_id=researcher.id,
        content="VIP customers qualify for instant 30-day refunds.",
    )
    billing_specialist = AgentDefinition(
        organization_id=org_id,
        name="BillingSpecialist",
        objective="Execute refunds safely",
        allowed_tools=["issue_refund"],
        credential_scopes=["billing:refund"],
        budget=AgentBudget(max_steps=3, max_cost_usd=0.5),
    )
    supervisor = AgentDefinition(
        organization_id=org_id,
        name="SupportSupervisor",
        objective="Resolve customer refund request",
        budget=AgentBudget(max_steps=3, max_cost_usd=1.0),
    )

    # 1. Successful supervisor -> researcher delegation + synthesis
    provider = _ScriptedProvider(
        [
            ModelCompletion(
                tool_calls=(AgentToolCall(tool_name="search_kb", arguments={"topic": "VIP"}),),
                input_tokens=60,
                output_tokens=20,
                cost_usd=0.001,
            ),
            ModelCompletion(
                content="Found 30-day VIP refund policy.",
                input_tokens=80,
                output_tokens=20,
                cost_usd=0.001,
            ),
            ModelCompletion(
                content="Supervisor summary: Refund approved under 30-day VIP policy.",
                input_tokens=90,
                output_tokens=25,
                cost_usd=0.002,
            ),
        ]
    )
    runtime = AgentRuntime(model_provider=provider, tool_gateway=gateway, memory_store=memory)
    team = SupervisorTeamRuntime(
        supervisor=supervisor,
        specialists=[researcher, billing_specialist],
        runtime=runtime,
    )
    res = await team.execute_delegations(
        organization_id=org_id,
        contracts=[
            DelegationContract(
                specialist_id=researcher.id,
                task="Check VIP refund policy",
                allowed_tools_subset=["search_kb"],
            )
        ],
    )
    assert res.status == "completed"
    assert "Supervisor summary" in res.output
    assert res.tool_calls == 1
    assert "Researcher" in res.structured_output["delegations"]

    # 2. High-risk tool triggers human approval pause cleanly via Tool Gateway
    approval_provider = _ScriptedProvider(
        [
            ModelCompletion(
                tool_calls=(AgentToolCall(tool_name="issue_refund", arguments={"amount": 250}),),
                input_tokens=40,
                output_tokens=15,
                cost_usd=0.001,
            )
        ]
    )
    approval_rt = AgentRuntime(model_provider=approval_provider, tool_gateway=gateway)
    paused = await approval_rt.run(billing_specialist, organization_id=org_id)
    assert paused.status == "waiting_approval"
    assert paused.pending_approval is not None
    assert paused.pending_approval["payload"]["tool"] == "issue_refund"

    # 3. Budget exhaustion triggers configured EscalationBehavior
    looping_provider = _ScriptedProvider(
        [
            ModelCompletion(
                tool_calls=(AgentToolCall(tool_name="search_kb", arguments={"topic": "loop"}),),
                input_tokens=10,
                output_tokens=10,
                cost_usd=0.001,
            )
        ]
        * 3
    )
    escalating_agent = researcher.model_copy(
        update={
            "budget": AgentBudget(max_steps=1),
            "escalation_behavior": EscalationBehavior.ESCALATE_HUMAN,
        }
    )
    esc_rt = AgentRuntime(model_provider=looping_provider, tool_gateway=gateway)
    esc_res = await esc_rt.run(escalating_agent, organization_id=org_id)
    assert esc_res.status == "escalated"


@pytest.mark.asyncio
async def test_email_slack_and_database_integration_nodes() -> None:
    org_id = uuid4()
    creds = InMemoryCredentialResolver()
    email_cred = creds.add(
        organization_id=org_id,
        credential_type="email_api",
        secrets={"api_key": "re_test_secret_123456"},
    )
    slack_cred = creds.add(
        organization_id=org_id,
        credential_type="slack_bot",
        secrets={"token": "xoxb-test-secret-123456"},
    )

    def _mock_transport(request: httpx.Request) -> httpx.Response:
        if "resend.com" in str(request.url):
            assert request.headers.get("idempotency-key") == "email-idem-1"
            return httpx.Response(200, json={"id": "msg_123"})
        if "slack.com" in str(request.url):
            return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000.1"})
        return httpx.Response(404)

    async def _db_exec(sql: str, params: dict[str, Any], max_rows: int) -> list[dict[str, Any]]:
        return [{"id": 1, "email": params["email"]}]

    harness = NodeTestHarness(
        organization_id=org_id,
        credential_resolver=creds,
        http_transport=httpx.MockTransport(_mock_transport),
        services={"db_executor": _db_exec},
    )

    email_out = await harness.run(
        SendEmailNode(),
        {
            "to": ["ceo@acme.io"],
            "subject": "Weekly Digest",
            "body_text": "All workflows healthy.",
        },
        credential_bindings={"provider": email_cred},
        idempotency_key="email-idem-1",
    )
    assert email_out.result.output["sent"] is True

    slack_out = await harness.run(
        SlackPostMessageNode(),
        {"channel": "#alerts", "text": "Deployment succeeded"},
        credential_bindings={"bot": slack_cred},
    )
    assert slack_out.result.output["ok"] is True
    assert slack_out.result.output["ts"] == "1700000.1"

    db_out = await harness.run(
        DatabaseQueryNode(),
        {
            "query": "SELECT id, email FROM customers WHERE email = :email",
            "params": {"email": "a@b.io"},
        },
    )
    assert db_out.result.output["row_count"] == 1

    # Read-only guard blocks destructive SQL & multi-statement injection
    with pytest.raises(SecurityPolicyError, match="Mutating SQL"):
        await harness.run(DatabaseQueryNode(), {"query": "DELETE FROM customers"})

    with pytest.raises(SecurityPolicyError, match="Multiple SQL"):
        await harness.run(DatabaseQueryNode(), {"query": "SELECT 1; DROP TABLE users"})


@pytest.mark.asyncio
async def test_agents_api_signed_webhooks_stripe_billing_scheduler_and_metrics(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "extensions.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_file.as_posix()}",
        auto_create_tables=True,
        webhook_signing_secret="wh_test_secret",
        stripe_webhook_secret="whsec_stripe_test",
    )
    app = create_app(settings)

    transport = ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
            boot = (
                await client.post(
                    "/api/v1/auth/bootstrap",
                    json={
                        "email": "ops@acme.io",
                        "organization_name": "Acme Ops",
                        "organization_slug": "acme-ops",
                    },
                )
            ).json()
            headers = {"Authorization": f"Bearer {boot['api_key']}"}

            # 1. Create and run a bounded AI agent via /api/v1/agents
            agent_resp = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "project_id": boot["project_id"],
                    "name": "Ops Triage Agent",
                    "objective": "Summarize incident status",
                },
            )
            assert agent_resp.status_code == 201
            agent_id = agent_resp.json()["id"]

            agent_run = await client.post(
                f"/api/v1/agents/{agent_id}/runs",
                headers=headers,
                json={"task_input": "Check payment latency"},
            )
            assert agent_run.status_code == 201
            assert agent_run.json()["status"] == "completed"

            # 2. Publish a webhook-triggered workflow and invoke via signed HMAC webhook
            wf = (
                await client.post(
                    "/api/v1/workflows",
                    headers=headers,
                    json={
                        "project_id": boot["project_id"],
                        "name": "Signed Hook Pipeline",
                        "graph": {
                            "nodes": [
                                {"id": "hook", "type": "core.webhook_trigger"},
                                {
                                    "id": "echo",
                                    "type": "core.set_data",
                                    "config": {
                                        "values": {"event": "{{ nodes.hook.output.body.event }}"}
                                    },
                                },
                            ],
                            "edges": [{"id": "e1", "source": "hook", "target": "echo"}],
                        },
                    },
                )
            ).json()
            wf_id = wf["id"]
            await client.post(
                f"/api/v1/workflows/{wf_id}/publish",
                headers=headers,
                json={"activate": True},
            )

            raw_payload = json.dumps({"event": "order.created"}).encode("utf-8")
            now_ts = int(time.time())
            bad_hook = await client.post(
                f"/api/v1/webhooks/{wf_id}",
                content=raw_payload,
                headers={
                    "X-FlowPilot-Signature": "v1=deadbeef",
                    "X-FlowPilot-Timestamp": str(now_ts),
                },
            )
            assert bad_hook.status_code == 401

            good_sig = compute_webhook_signature("wh_test_secret", now_ts, raw_payload)
            ok_hook = await client.post(
                f"/api/v1/webhooks/{wf_id}",
                content=raw_payload,
                headers={
                    "X-FlowPilot-Signature": f"t={now_ts},v1={good_sig}",
                    "Idempotency-Key": "hook-evt-1",
                },
            )
            assert ok_hook.status_code == 202
            assert ok_hook.json()["status"] == "SUCCEEDED"

            # 3. Idempotent Stripe billing webhook upgrades org plan to 'pro'
            stripe_event = json.dumps(
                {
                    "id": "evt_stripe_1001",
                    "type": "customer.subscription.updated",
                    "data": {
                        "object": {
                            "id": "sub_123",
                            "customer": "cus_123",
                            "status": "active",
                            "metadata": {
                                "organization_id": boot["organization_id"],
                                "plan": "pro",
                            },
                        }
                    },
                }
            ).encode("utf-8")
            stripe_sig = compute_webhook_signature("whsec_stripe_test", now_ts, stripe_event)
            stripe_headers = {"Stripe-Signature": f"t={now_ts},v1={stripe_sig}"}

            first_bill = await client.post(
                "/api/v1/billing/webhooks/stripe",
                content=stripe_event,
                headers=stripe_headers,
            )
            assert first_bill.status_code == 200
            assert first_bill.json()["plan"] == "pro"
            assert first_bill.json()["idempotent_replay"] is False

            # Replay of exact same Stripe event is idempotently ignored
            replay_bill = await client.post(
                "/api/v1/billing/webhooks/stripe",
                content=stripe_event,
                headers=stripe_headers,
            )
            assert replay_bill.status_code == 200
            assert replay_bill.json()["idempotent_replay"] is True

            summary = (await client.get("/api/v1/billing/summary", headers=headers)).json()
            assert summary["plan"]["tier"] == "pro"
            assert summary["plan"]["monthly_price_usd"] == 49

            # 4. Cron trigger scheduler dispatches once per minute bucket
            assert cron_matches("*/5 * * * *", datetime(2026, 11, 25, 10, 15, tzinfo=UTC))
            assert not cron_matches("*/5 * * * *", datetime(2026, 11, 25, 10, 16, tzinfo=UTC))

            async with app.state.session_factory() as session:
                session.add(
                    WorkflowTriggerModel(
                        organization_id=UUID(wf["organization_id"]),
                        workflow_id=UUID(wf_id),
                        trigger_type="schedule",
                        config={"cron": "*/15 * * * *"},
                        enabled=True,
                    )
                )
                await session.commit()

            scheduler = TriggerScheduler(
                session_factory=app.state.session_factory,
                engine=app.state.engine,
            )
            tick_time = datetime(2026, 11, 25, 12, 30, tzinfo=UTC)
            first_tick = await scheduler.tick(now=tick_time)
            second_tick = await scheduler.tick(now=tick_time)
            assert len(first_tick) == 1
            assert second_tick == first_tick  # Deterministic minute-bucket idempotency!

            # 5. Prometheus /metrics & OpenTelemetry trace helpers
            metrics_resp = await client.get("/metrics")
            assert metrics_resp.status_code == 200
            assert "flowpilot_http_requests_total" in metrics_resp.text

            reg = MetricsRegistry()
            reg.inc("flowpilot_test_counter", pool="workflow")
            assert 'flowpilot_test_counter{pool="workflow"} 1.0' in reg.render_prometheus()
            log_line = format_structured_log("info", "started", run_id="r1")
            assert '"run_id":"r1"' in log_line
            with trace_workflow_run(
                organization_id="o1",
                workflow_id="w1",
                workflow_version_id="v1",
                run_id="r1",
            ), trace_node_execution(
                run_id="r1",
                node_id="n1",
                node_type="core.set_data",
                worker_pool="workflow",
                attempt=1,
            ):
                pass
