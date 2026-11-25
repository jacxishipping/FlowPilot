"""Tests for flowpilot_expressions parser, evaluator, security, and static inspector."""

from __future__ import annotations

import pytest

from flowpilot_expressions import (
    ExpressionEvaluationError,
    ExpressionSecurityError,
    ExpressionSyntaxError,
    evaluate_expression,
    inspect_value,
    resolve_template,
    resolve_value,
)


def test_spec_example_and_nested_resolution() -> None:
    ctx = {
        "nodes": {
            "customer_lookup": {
                "output": {
                    "email": "  Alice@Example.com ",
                    "tier": "pro",
                    "score": 92,
                    "items": [{"sku": "A1", "price": 19.5}, {"sku": "B2", "price": 30.5}],
                }
            },
            "billing-step": {"output": {"paid": True}},
        },
        "trigger": {"user_id": "u_42"},
    }

    assert (
        resolve_template("{{ nodes.customer_lookup.output.email }}", ctx)
        == "  Alice@Example.com "
    )
    assert (
        resolve_template("{{ nodes.customer_lookup.output.email | trim | lower }}", ctx)
        == "alice@example.com"
    )
    # Single expression preserves native non-string types
    assert resolve_template("{{ nodes.customer_lookup.output.score }}", ctx) == 92
    assert resolve_template("{{ nodes.billing-step.output.paid }}", ctx) is True

    # Mixed template interpolates to string
    greeting = resolve_template(
        "User {{ trigger.user_id }} -> {{ nodes.customer_lookup.output.items[0].sku }}",
        ctx,
    )
    assert greeting == "User u_42 -> A1"

    total_expr = (
        "{{ nodes.customer_lookup.output.items[0].price + "
        "nodes.customer_lookup.output.items[1].price }}"
    )
    vip_expr = (
        "{{ nodes.customer_lookup.output.score >= 90 and "
        "nodes.customer_lookup.output.tier == 'pro' }}"
    )
    resolved = resolve_value(
        {
            "total": total_expr,
            "is_vip": vip_expr,
            "fallback": "{{ nodes.missing.output.x ?? 'default_val' }}",
            "label": "{{ nodes.customer_lookup.output.score > 80 ? 'high' : 'low' }}",
            "skus": "{{ ['A1', 'B2'] | join('-') }}",
        },
        ctx,
    )
    assert resolved == {
        "total": 50.0,
        "is_vip": True,
        "fallback": "default_val",
        "label": "high",
        "skus": "A1-B2",
    }


def test_expression_security_blocks_code_execution_and_dunders() -> None:
    ctx = {"nodes": {"a": {"output": {"x": 1}}}}

    with pytest.raises(ExpressionSecurityError):
        evaluate_expression("nodes.__class__.__mro__", ctx)

    with pytest.raises(ExpressionSecurityError):
        evaluate_expression("nodes.a['__dict__']", ctx)

    with pytest.raises(ExpressionSecurityError):
        evaluate_expression("eval('1+1')", ctx)

    with pytest.raises(ExpressionSecurityError):
        evaluate_expression("os.system('echo hi')", ctx)

    with pytest.raises(ExpressionSecurityError):
        evaluate_expression("nodes.a.output.x | exec", ctx)

    deep_expr = "(" * 34 + "1" + ")" * 34
    with pytest.raises(ExpressionSecurityError):
        evaluate_expression(deep_expr, ctx)


def test_division_by_zero_and_syntax_errors() -> None:
    with pytest.raises(ExpressionEvaluationError):
        evaluate_expression("10 / 0", {})

    with pytest.raises(ExpressionSyntaxError):
        evaluate_expression("nodes.a.output +", {})


def test_static_inspector_extracts_node_refs_and_flags_issues() -> None:
    config = {
        "to": "{{ nodes.customer_lookup.output.email | trim }}",
        "body": "Order {{ nodes['fetch_order'].output.id }} from {{ trigger.source }}",
        "nested": [{"x": "{{ nodes.step_a.output.v + nodes.step_b.output.v }}"}],
    }
    analysis = inspect_value(config)
    assert analysis.issues == []
    assert analysis.node_refs == {"customer_lookup", "fetch_order", "step_a", "step_b"}
    assert "trigger" in analysis.root_refs
    assert not analysis.uses_dynamic_node_ref

    bad = inspect_value({"broken": "{{ nodes.a.output", "dyn": "{{ nodes[trigger.key].output }}"})
    assert len(bad.issues) == 1
    assert bad.uses_dynamic_node_ref
