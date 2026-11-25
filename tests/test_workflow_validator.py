"""Tests for flowpilot_validator pre-publish DAG, expression, schema, and policy rules."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

from flowpilot import (
    BaseNode,
    CredentialRequirement,
    EmptyConfig,
    ExecutionContext,
    NodeMetadata,
    NodeResult,
)
from flowpilot_nodes_core import create_default_registry
from flowpilot_schema import (
    EdgeDefinition,
    EdgeKind,
    LoopSpec,
    NodeDefinition,
    NodePolicy,
    RetryPolicy,
    SideEffectPolicy,
    WorkflowDefinition,
    WorkflowGraph,
)
from flowpilot_validator import WorkflowValidator


class _PaymentChargeNode(BaseNode[EmptyConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="billing.charge_card",
        name="Charge Card",
        side_effect=SideEffectPolicy.IRREVERSIBLE,
        credentials=(
            CredentialRequirement(slot="stripe", credential_type="stripe_api", required=True),
        ),
        required_permissions=("billing.charge",),
    )
    config_model: ClassVar[type[EmptyConfig]] = EmptyConfig

    async def execute(self, config: EmptyConfig, ctx: ExecutionContext) -> NodeResult:
        return NodeResult(output={"charged": True})


def _make_validator() -> WorkflowValidator:
    registry = create_default_registry()
    registry.register(_PaymentChargeNode)
    return WorkflowValidator(registry)


def test_valid_workflow_with_condition_and_loop_passes() -> None:
    validator = _make_validator()
    wf = WorkflowDefinition(
        name="Customer Notification Pipeline",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="prepare",
                    type="core.set_data",
                    config={"values": {"items": [1, 2, 3], "send": True}},
                ),
                NodeDefinition(
                    id="check",
                    type="core.condition",
                    config={"condition": "{{ nodes.prepare.output.send }}"},
                ),
                NodeDefinition(
                    id="batch",
                    type="core.loop",
                    loop=LoopSpec(
                        items_expression="{{ nodes.prepare.output.items }}",
                        max_iterations=50,
                        body_graph=WorkflowGraph(
                            nodes=[
                                NodeDefinition(
                                    id="double_item",
                                    type="core.set_data",
                                    config={"values": {"val": "{{ loop.item * 2 }}"}},
                                )
                            ]
                        ),
                    ),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="prepare"),
                EdgeDefinition(id="e2", source="prepare", target="check"),
                EdgeDefinition(id="e3", source="check", target="batch", kind=EdgeKind.TRUE),
            ],
        ),
        output_mapping={"results": "{{ nodes.batch.output.results }}"},
    )
    res = validator.validate(wf)
    assert res.is_valid, [i.model_dump() for i in res.issues]


def test_rejects_arbitrary_cycle_and_reports_cycle_path() -> None:
    validator = _make_validator()
    wf = WorkflowDefinition(
        name="Cyclic Graph",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(id="a", type="core.set_data"),
                NodeDefinition(id="b", type="core.set_data"),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="a"),
                EdgeDefinition(id="e2", source="a", target="b"),
                EdgeDefinition(id="e3", source="b", target="a"),
            ],
        ),
    )
    res = validator.validate(wf)
    assert not res.is_valid
    assert "GRAPH_CYCLE_DETECTED" in res.error_codes()


def test_rejects_non_upstream_and_self_expression_references() -> None:
    validator = _make_validator()
    wf = WorkflowDefinition(
        name="Bad References",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="first",
                    type="core.set_data",
                    config={
                        "values": {
                            "future": "{{ nodes.second.output.x }}",
                            "self_ref": "{{ nodes.first.output.y }}",
                            "ghost": "{{ nodes.does_not_exist.output.z }}",
                        }
                    },
                ),
                NodeDefinition(id="second", type="core.set_data", config={"values": {"x": 1}}),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="first"),
                EdgeDefinition(id="e2", source="first", target="second"),
            ],
        ),
    )
    res = validator.validate(wf)
    codes = res.error_codes()
    assert "NON_UPSTREAM_NODE_REFERENCE" in codes
    assert "SELF_NODE_REFERENCE" in codes
    assert "UNKNOWN_NODE_REFERENCE" in codes


def test_rejects_secrets_missing_credentials_permissions_and_unsafe_retry() -> None:
    validator = _make_validator()
    wf = WorkflowDefinition(
        name="Security Policy Violations",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="leak",
                    type="core.http_request",
                    config={
                        "method": "GET",
                        "url": "https://api.example.com/v1/test",
                        "headers": {"Authorization": "Bearer sk-1234567890abcdefghij1234"},
                    },
                ),
                NodeDefinition(
                    id="charge",
                    type="billing.charge_card",
                    # Missing required credential 'stripe' and idempotency key for retry
                    policy=NodePolicy(
                        retry=RetryPolicy(
                            max_attempts=3,
                            allow_retry_with_idempotency_key=True,
                        )
                    ),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="leak"),
                EdgeDefinition(id="e2", source="leak", target="charge"),
            ],
        ),
    )
    res = validator.validate(wf, granted_permissions={"http.outbound"})
    codes = res.error_codes()
    assert "PLAINTEXT_SECRET_DETECTED" in codes
    assert "MISSING_REQUIRED_CREDENTIAL" in codes
    assert "MISSING_PERMISSION" in codes
    assert "IRREVERSIBLE_RETRY_WITHOUT_IDEMPOTENCY_KEY" in codes

    # Once credential, permission, and idempotency key are supplied, charge node validates cleanly
    charge_ok = WorkflowDefinition(
        name="Safe Charge",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="charge",
                    type="billing.charge_card",
                    credentials={"stripe": uuid4()},
                    policy=NodePolicy(
                        idempotency_key_expression="charge-{{ trigger.order_id }}",
                        retry=RetryPolicy(
                            max_attempts=3,
                            allow_retry_with_idempotency_key=True,
                        ),
                    ),
                ),
            ],
            edges=[EdgeDefinition(id="e1", source="start", target="charge")],
        ),
    )
    ok_res = validator.validate(charge_ok, granted_permissions={"billing.charge"})
    assert ok_res.is_valid, [i.model_dump() for i in ok_res.issues]


def test_loop_body_rejects_triggers_and_invalid_edge_branch() -> None:
    validator = _make_validator()
    wf = WorkflowDefinition(
        name="Bad Loop and Edge",
        graph=WorkflowGraph(
            nodes=[
                NodeDefinition(id="start", type="core.manual_trigger"),
                NodeDefinition(
                    id="bad_loop",
                    type="core.loop",
                    loop=LoopSpec(
                        items_expression="{{ trigger.items }}",
                        body_graph=WorkflowGraph(
                            nodes=[NodeDefinition(id="inner_trig", type="core.manual_trigger")]
                        ),
                    ),
                ),
            ],
            edges=[
                EdgeDefinition(id="e1", source="start", target="bad_loop", kind=EdgeKind.TRUE),
            ],
        ),
    )
    res = validator.validate(wf)
    codes = res.error_codes()
    assert "INVALID_EDGE_KIND" in codes
    assert "LOOP_BODY_CONTAINS_TRIGGER" in codes
