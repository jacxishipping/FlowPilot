"""End-to-end tests for flowpilot_engine covering branching, loops, retries, idempotency,
error branches, approvals, cancellation, artifacts, HTTP, and bounded AI agents."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest

from flowpilot import (
    BaseNode,
    EmptyConfig,
    ExecutionContext,
    InMemoryCredentialResolver,
    NodeMetadata,
    NodeResult,
    NonRetryableNodeError,
    RetryableNodeError,
)
from flowpilot_engine import (
    InMemoryEventSink,
    WorkflowExecutionEngine,
    WorkflowExecutionError,
    format_sse,
)
from flowpilot_nodes_core import (
    AgentToolCall,
    ModelCompletion,
    create_default_registry,
)
from flowpilot_schema import (
    ApprovalDecision,
    EdgeDefinition,
    EdgeKind,
    EventType,
    LoopSpec,
    NodeDefinition,
    NodePolicy,
    NodeStatus,
    RetryPolicy,
    RunStatus,
    SideEffectPolicy,
    WorkflowDefinition,
    WorkflowGraph,
    WorkflowSettings,
    WorkflowVersion,
)


async def _instant_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_branching_skip_propagation_and_ordered_sse_events() -> None:
    registry = create_default_registry()
    sink = InMemoryEventSink()
    engine = WorkflowExecutionEngine(registry=registry, event_sink=sink, sleep_fn=_instant_sleep)

    wf = WorkflowDefinition(
        name="VIP Router",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="is_vip",
                    type="core.condition",
                    config={"condition": "{{ nodes.start.output.spend >= 100 }}"},
                ),
                NodeDefinition(
                    id="vip_path",
                    type="core.set_data",
                    config={
                        "values": {
                            "tier": "VIP",
                            "email": "{{ nodes.start.output.email | lower }}",
                        }
                    },
                ),
                NodeDefinition(
                    id="std_path",
                    type="core.set_data",
                    config={"values": {"tier": "STANDARD"}},
                ),
                NodeDefinition(
                    id="after_std",
                    type="core.set_data",
                    config={"values": {"note": "only for standard"}},
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="is_vip"),
                EdgeDefinition(id="e2", source="is_vip", target="vip_path", kind=EdgeKind.TRUE),
                EdgeDefinition(id="e3", source="is_vip", target="std_path", kind=EdgeKind.FALSE),
                EdgeDefinition(id="e4", source="std_path", target="after_std"),
            ],
        ),
        output_mapping={
            "tier": "{{ nodes.vip_path.output.tier }}",
            "email": "{{ nodes.vip_path.output.email }}",
        },
    )
    version = WorkflowVersion.from_definition(wf, version_number=1)

    run = await engine.start_run(
        version,
        trigger_input={"spend": 250, "email": "Founder@Acme.IO"},
    )
    assert run.status == RunStatus.SUCCEEDED
    assert run.node_executions["vip_path"].status == NodeStatus.SUCCEEDED
    assert run.node_executions["std_path"].status == NodeStatus.SKIPPED
    assert run.node_executions["after_std"].status == NodeStatus.SKIPPED

    output = await engine.payloads.unpack(run.output_ref)
    assert output == {"tier": "VIP", "email": "founder@acme.io"}

    events = sink.get_events(run.id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert events[0].event_type == EventType.RUN_CREATED
    assert events[-1].event_type == EventType.RUN_SUCCEEDED
    sse_frame = format_sse(events[0])
    assert "event: run.created" in sse_frame


@pytest.mark.asyncio
async def test_bounded_loop_execution_and_max_iteration_cap() -> None:
    registry = create_default_registry()
    engine = WorkflowExecutionEngine(registry=registry, sleep_fn=_instant_sleep)

    wf = WorkflowDefinition(
        name="Batch Double",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="loop_items",
                    type="core.loop",
                    loop=LoopSpec(
                        items_expression="{{ nodes.start.output.numbers }}",
                        max_iterations=3,
                        body_graph=WorkflowGraph(
                            nodes=[
                                NodeDefinition(
                                    id="calc",
                                    type="core.set_data",
                                    config={
                                        "values": {
                                            "idx": "{{ loop.index }}",
                                            "doubled": "{{ loop.item * 2 }}",
                                        }
                                    },
                                )
                            ]
                        ),
                    ),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="loop_items")],
        ),
    )
    version = WorkflowVersion.from_definition(wf, version_number=1)

    ok_run = await engine.start_run(version, trigger_input={"numbers": [5, 10, 15]})
    assert ok_run.status == RunStatus.SUCCEEDED
    loop_out = await engine.payloads.unpack(ok_run.node_executions["loop_items"].output_ref)
    assert loop_out["count"] == 3
    assert [r["calc"]["doubled"] for r in loop_out["results"]] == [10, 20, 30]

    # Exceeding max_iterations fails safely
    too_many = await engine.start_run(version, trigger_input={"numbers": [1, 2, 3, 4]})
    assert too_many.status == RunStatus.FAILED
    assert too_many.error is not None
    assert too_many.error.code == "LOOP_MAX_ITERATIONS_EXCEEDED"


@pytest.mark.asyncio
async def test_retry_backoff_and_irreversible_side_effect_idempotency_guard() -> None:
    attempts: dict[str, int] = {"flaky_safe": 0, "flaky_irrev_unsafe": 0, "flaky_irrev_idem": 0}

    class _FlakySafeNode(BaseNode[EmptyConfig, Any]):
        metadata: ClassVar[NodeMetadata] = NodeMetadata(
            type="test.flaky_safe",
            name="Flaky Safe",
            side_effect=SideEffectPolicy.IDEMPOTENT,
        )
        config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

        async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
            attempts["flaky_safe"] += 1
            if attempts["flaky_safe"] < 3:
                raise RetryableNodeError("temporary 503")
            return NodeResult(output={"attempts": attempts["flaky_safe"]})

    class _FlakyIrreversibleNode(BaseNode[EmptyConfig, Any]):
        metadata: ClassVar[NodeMetadata] = NodeMetadata(
            type="test.flaky_irreversible",
            name="Flaky Payment",
            side_effect=SideEffectPolicy.IRREVERSIBLE,
        )
        config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

        async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
            key = "flaky_irrev_idem" if ctx.idempotency_key else "flaky_irrev_unsafe"
            attempts[key] += 1
            if attempts[key] < 2:
                raise RetryableNodeError("gateway timeout")
            return NodeResult(output={"idem_key": ctx.idempotency_key, "attempts": attempts[key]})

    registry = create_default_registry()
    registry.register(_FlakySafeNode)
    registry.register(_FlakyIrreversibleNode)
    engine = WorkflowExecutionEngine(registry=registry, sleep_fn=_instant_sleep, rng_seed=42)

    # 1. Idempotent flaky node retries and succeeds on attempt 3
    wf_safe = WorkflowDefinition(
        name="Safe Retry",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="step",
                    type="test.flaky_safe",
                    policy=NodePolicy(retry=RetryPolicy(max_attempts=3)),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="step")],
        ),
    )
    run_safe = await engine.start_run(WorkflowVersion.from_definition(wf_safe, version_number=1))
    assert run_safe.status == RunStatus.SUCCEEDED
    assert run_safe.node_executions["step"].attempt == 3

    # 2. Irreversible node WITHOUT allow_retry_with_idempotency_key NEVER retries blindly
    wf_unsafe = WorkflowDefinition(
        name="Unsafe Irreversible",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="pay",
                    type="test.flaky_irreversible",
                    policy=NodePolicy(
                        retry=RetryPolicy(
                            max_attempts=3,
                            allow_retry_with_idempotency_key=False,
                        )
                    ),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="pay")],
        ),
    )
    unsafe_version = WorkflowVersion.from_definition(wf_unsafe, version_number=1)
    run_unsafe = await engine.start_run(unsafe_version)
    assert run_unsafe.status == RunStatus.FAILED
    assert attempts["flaky_irrev_unsafe"] == 1  # Strictly NO blind retry!

    # 3. Irreversible node WITH idempotency key & explicit opt-in retries safely
    wf_idem = WorkflowDefinition(
        name="Idempotent Irreversible",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="pay",
                    type="test.flaky_irreversible",
                    policy=NodePolicy(
                        idempotency_key_expression="pay-{{ trigger.invoice_id }}",
                        retry=RetryPolicy(max_attempts=3, allow_retry_with_idempotency_key=True),
                    ),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="pay")],
        ),
    )
    run_idem = await engine.start_run(
        WorkflowVersion.from_definition(wf_idem, version_number=1),
        trigger_input={"invoice_id": "inv_99"},
    )
    assert run_idem.status == RunStatus.SUCCEEDED
    assert attempts["flaky_irrev_idem"] == 2
    pay_out = await engine.payloads.unpack(run_idem.node_executions["pay"].output_ref)
    assert pay_out["idem_key"] == "pay-inv_99"


@pytest.mark.asyncio
async def test_error_branch_recovers_without_failing_run() -> None:
    class _AlwaysFailNode(BaseNode[EmptyConfig, Any]):
        metadata: ClassVar[NodeMetadata] = NodeMetadata(type="test.always_fail", name="Fail")
        config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

        async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
            raise NonRetryableNodeError("upstream 404", code="NOT_FOUND")

    registry = create_default_registry()
    registry.register(_AlwaysFailNode)
    engine = WorkflowExecutionEngine(registry=registry, sleep_fn=_instant_sleep)

    wf = WorkflowDefinition(
        name="Error Recovery",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(id="boom", type="test.always_fail"),
                NodeDefinition(
                    id="recover",
                    type="core.error_handler",
                    config={
                        "captured_error": "{{ error.code }}",
                        "fallback_output": {"status": "degraded"},
                    },
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="boom"),
                EdgeDefinition(id="e2", source="boom", target="recover", kind=EdgeKind.ERROR),
            ],
        ),
    )
    run = await engine.start_run(WorkflowVersion.from_definition(wf, version_number=1))
    assert run.status == RunStatus.SUCCEEDED
    assert run.node_executions["boom"].status == NodeStatus.FAILED
    assert run.node_executions["recover"].status == NodeStatus.SUCCEEDED
    rec_out = await engine.payloads.unpack(run.node_executions["recover"].output_ref)
    assert rec_out["error"] == "NOT_FOUND"
    assert rec_out["fallback"] == {"status": "degraded"}


@pytest.mark.asyncio
async def test_human_approval_pause_resume_and_tenant_enforcement() -> None:
    upstream_calls = 0

    class _CountingPrepNode(BaseNode[EmptyConfig, Any]):
        metadata: ClassVar[NodeMetadata] = NodeMetadata(type="test.prep", name="Prep")
        config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

        async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
            nonlocal upstream_calls
            upstream_calls += 1
            return NodeResult(output={"amount": 5000})

    registry = create_default_registry()
    registry.register(_CountingPrepNode)
    engine = WorkflowExecutionEngine(registry=registry, sleep_fn=_instant_sleep)

    org_id = uuid4()
    other_org_id = uuid4()
    wf = WorkflowDefinition(
        organization_id=org_id,
        name="Wire Transfer Approval",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(id="prep", type="test.prep"),
                NodeDefinition(
                    id="approve_wire",
                    type="core.human_approval",
                    config={
                        "title": "Approve wire of ${{ nodes.prep.output.amount }}",
                        "payload": {"amount": "{{ nodes.prep.output.amount }}"},
                    },
                ),
                NodeDefinition(
                    id="execute_wire",
                    type="core.set_data",
                    config={
                        "values": {
                            "wired": True,
                            "approver": "{{ nodes.approve_wire.output.decided_by }}",
                        }
                    },
                ),
                NodeDefinition(
                    id="notify_rejected",
                    type="core.set_data",
                    config={"values": {"wired": False}},
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="prep"),
                EdgeDefinition(id="e2", source="prep", target="approve_wire"),
                EdgeDefinition(
                    id="e3",
                    source="approve_wire",
                    target="execute_wire",
                    kind=EdgeKind.TRUE,
                ),
                EdgeDefinition(
                    id="e4",
                    source="approve_wire",
                    target="notify_rejected",
                    kind=EdgeKind.FALSE,
                ),
            ],
        ),
    )
    version = WorkflowVersion.from_definition(wf, version_number=1)

    # Cross-tenant start_run must be rejected
    with pytest.raises(WorkflowExecutionError, match="Tenant isolation"):
        await engine.start_run(version, organization_id=other_org_id)

    paused_run = await engine.start_run(version, organization_id=org_id)
    assert paused_run.status == RunStatus.WAITING_APPROVAL
    assert upstream_calls == 1
    assert len(paused_run.approvals) == 1
    approval_id, approval = next(iter(paused_run.approvals.items()))
    assert approval.decision == ApprovalDecision.PENDING
    assert approval.payload == {"amount": 5000}

    # Cross-tenant approval decision must be rejected
    with pytest.raises(WorkflowExecutionError, match="Tenant isolation"):
        await engine.submit_approval_decision(
            paused_run.id,
            approval_id,
            organization_id=other_org_id,
            approved=True,
            decided_by="attacker",
        )

    # Authorized approval resumes from checkpoint WITHOUT re-running 'prep'
    resumed = await engine.submit_approval_decision(
        paused_run.id,
        approval_id,
        organization_id=org_id,
        approved=True,
        decided_by="cfo@acme.io",
        comment="Verified invoice",
    )
    assert resumed.status == RunStatus.SUCCEEDED
    assert upstream_calls == 1  # Checkpoint preserved!
    assert resumed.node_executions["execute_wire"].status == NodeStatus.SUCCEEDED
    assert resumed.node_executions["notify_rejected"].status == NodeStatus.SKIPPED
    wire_out = await engine.payloads.unpack(resumed.node_executions["execute_wire"].output_ref)
    assert wire_out == {"wired": True, "approver": "cfo@acme.io"}

    # Duplicate decision attempt is rejected
    with pytest.raises(WorkflowExecutionError):
        await engine.submit_approval_decision(
            paused_run.id,
            approval_id,
            organization_id=org_id,
            approved=False,
            decided_by="cfo@acme.io",
        )


@pytest.mark.asyncio
async def test_idempotent_run_dedup_cancellation_and_large_payload_externalization() -> None:
    registry = create_default_registry()
    engine = WorkflowExecutionEngine(registry=registry, sleep_fn=_instant_sleep)

    wf = WorkflowDefinition(
        name="Large Artifact & Dedup",
        settings=WorkflowSettings(payload_inline_limit_bytes=1024),
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="big_data",
                    type="core.set_data",
                    config={"values": {"blob": "A" * 2048}},
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="big_data")],
        ),
    )
    version = WorkflowVersion.from_definition(wf, version_number=1)

    run1 = await engine.start_run(version, idempotency_key="webhook-evt-100")
    run2 = await engine.start_run(version, idempotency_key="webhook-evt-100")
    assert run1.id == run2.id  # Deduplicated!
    assert run1.status == RunStatus.SUCCEEDED

    big_ref = run1.node_executions["big_data"].output_ref
    assert big_ref is not None
    assert big_ref.is_external  # Stored in ArtifactStore, NOT inline!
    assert big_ref.artifact is not None
    unpacked = await engine.payloads.unpack(big_ref)
    assert len(unpacked["blob"]) == 2048

    # Cancel a paused run
    approval_wf = WorkflowDefinition(
        name="Cancelable Approval",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="gate",
                    type="core.human_approval",
                    config={"title": "Wait forever"},
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="gate")],
        ),
    )
    paused = await engine.start_run(WorkflowVersion.from_definition(approval_wf, version_number=1))
    assert paused.status == RunStatus.WAITING_APPROVAL
    canceled = await engine.cancel_run(paused.id, organization_id=paused.organization_id)
    assert canceled.status == RunStatus.CANCELED
    assert canceled.node_executions["gate"].status == NodeStatus.CANCELED


@pytest.mark.asyncio
async def test_http_node_and_bounded_agent_tool_gateway_and_metering() -> None:
    org_id = uuid4()
    cred_resolver = InMemoryCredentialResolver()
    api_cred_id = cred_resolver.add(
        organization_id=org_id,
        credential_type="http_auth",
        secrets={"token": "secret-bearer-token-xyz"},
    )

    seen_headers: dict[str, str] = {}

    def _mock_http(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, json={"customer": "Acme Corp", "status": "active"})

    class _FakeModelProvider:
        def __init__(self, script: list[ModelCompletion]) -> None:
            self._script = list(script)
            self._idx = 0

        async def complete(self, **kwargs: Any) -> ModelCompletion:
            res = self._script[self._idx]
            self._idx += 1
            return res

    async def _lookup_ticket(args: dict[str, Any], ctx: ExecutionContext) -> dict[str, Any]:
        return {"ticket_id": args["id"], "priority": "P1"}

    provider = _FakeModelProvider(
        [
            ModelCompletion(
                tool_calls=(AgentToolCall(tool_name="lookup_ticket", arguments={"id": "T-7"}),),
                input_tokens=120,
                output_tokens=30,
                cost_usd=0.002,
            ),
            ModelCompletion(
                content="Ticket T-7 is P1 for active customer Acme Corp.",
                input_tokens=180,
                output_tokens=40,
                cost_usd=0.003,
            ),
        ]
    )

    registry = create_default_registry()
    engine = WorkflowExecutionEngine(
        registry=registry,
        credential_resolver=cred_resolver,
        http_transport=httpx.MockTransport(_mock_http),
        services={"model_provider": provider, "agent_tools": {"lookup_ticket": _lookup_ticket}},
        sleep_fn=_instant_sleep,
    )

    wf = WorkflowDefinition(
        organization_id=org_id,
        name="Customer Triage Agent",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="fetch_customer",
                    type="core.http_request",
                    config={"method": "GET", "url": "https://crm.example.com/customers/1"},
                    credentials={"auth": api_cred_id},
                    policy=NodePolicy(
                        allowed_domains=["crm.example.com"],
                        idempotency_key_expression="crm-{{ trigger.req_id }}",
                    ),
                ),
                NodeDefinition(
                    id="triage_agent",
                    type="core.bounded_agent",
                    config={
                        "objective": (
                            "Triage customer {{ nodes.fetch_customer.output.body.customer }}"
                        ),
                        "allowed_tools": ["lookup_ticket"],
                        "max_steps": 4,
                        "max_tool_calls": 3,
                        "max_tokens": 2000,
                        "max_cost_usd": 0.50,
                    },
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="fetch_customer"),
                EdgeDefinition(id="e2", source="fetch_customer", target="triage_agent"),
            ],
        ),
    )

    run = await engine.start_run(
        WorkflowVersion.from_definition(wf, version_number=1),
        organization_id=org_id,
        trigger_input={"req_id": "req-55"},
    )
    assert run.status == RunStatus.SUCCEEDED
    assert seen_headers.get("authorization") == "Bearer secret-bearer-token-xyz"
    assert seen_headers.get("idempotency-key") == "crm-req-55"

    agent_out = await engine.payloads.unpack(run.node_executions["triage_agent"].output_ref)
    assert "Ticket T-7 is P1" in agent_out["result"]
    assert agent_out["steps"] == 2
    assert agent_out["tool_calls"] == 1

    metrics = {r.metric for r in run.usage_records}
    expected_metrics = {
        "workflow_run",
        "node_execution",
        "agent_steps",
        "tool_calls",
        "ai_tokens_input",
        "ai_tokens_output",
    }
    assert expected_metrics <= metrics

    # Verify tool gateway blocks disallowed tools strictly
    rogue_provider = _FakeModelProvider(
        [
            ModelCompletion(
                tool_calls=(AgentToolCall(tool_name="delete_database", arguments={}),),
                input_tokens=50,
                output_tokens=10,
                cost_usd=0.001,
            )
        ]
    )
    rogue_engine = WorkflowExecutionEngine(
        registry=registry,
        services={"model_provider": rogue_provider},
        sleep_fn=_instant_sleep,
    )
    rogue_wf = WorkflowDefinition(
        name="Rogue Agent Guard",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="agent",
                    type="core.bounded_agent",
                    config={
                        "objective": "Do something",
                        "allowed_tools": ["lookup_ticket"],
                    },
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="agent")],
        ),
    )
    rogue_version = WorkflowVersion.from_definition(rogue_wf, version_number=1)
    rogue_run = await rogue_engine.start_run(rogue_version)
    assert rogue_run.status == RunStatus.FAILED
    assert rogue_run.error is not None
    assert rogue_run.error.code == "AGENT_TOOL_NOT_ALLOWED"
