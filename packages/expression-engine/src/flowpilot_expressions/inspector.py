"""Static AST analyzer for extracting node references and validating expression syntax."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flowpilot_expressions.ast_parser import (
    AttrNode,
    BinaryNode,
    ExprNode,
    IdentifierNode,
    IndexNode,
    ListNode,
    LiteralNode,
    PipeNode,
    TernaryNode,
    UnaryNode,
    parse_expression,
)
from flowpilot_expressions.errors import ExpressionError
from flowpilot_expressions.evaluator import ALLOWED_ROOTS, SAFE_FILTERS, TEMPLATE_PATTERN


@dataclass
class ExpressionIssue:
    """Static syntax or security violation discovered while inspecting expressions."""

    path: str
    expression: str
    message: str


@dataclass
class ExpressionAnalysis:
    """Result of statically inspecting expressions inside a node config or value tree."""

    expressions: list[str] = field(default_factory=list)
    node_refs: set[str] = field(default_factory=set)
    root_refs: set[str] = field(default_factory=set)
    uses_dynamic_node_ref: bool = False
    issues: list[ExpressionIssue] = field(default_factory=list)


def _walk_ast(node: ExprNode, analysis: ExpressionAnalysis, path: str, raw_expr: str) -> None:
    if isinstance(node, IdentifierNode):
        analysis.root_refs.add(node.name)
        if node.name not in ALLOWED_ROOTS:
            analysis.issues.append(
                ExpressionIssue(
                    path=path,
                    expression=raw_expr,
                    message=(
                        f"Disallowed root identifier '{node.name}'. "
                        f"Allowed roots: {sorted(ALLOWED_ROOTS)}."
                    ),
                )
            )
        return

    if isinstance(node, AttrNode):
        if isinstance(node.target, IdentifierNode) and node.target.name == "nodes":
            analysis.root_refs.add("nodes")
            analysis.node_refs.add(node.attribute)
            return
        _walk_ast(node.target, analysis, path, raw_expr)
        return

    if isinstance(node, IndexNode):
        if isinstance(node.target, IdentifierNode) and node.target.name == "nodes":
            analysis.root_refs.add("nodes")
            if isinstance(node.index, LiteralNode) and isinstance(node.index.value, str):
                analysis.node_refs.add(node.index.value)
            else:
                analysis.uses_dynamic_node_ref = True
            _walk_ast(node.index, analysis, path, raw_expr)
            return
        _walk_ast(node.target, analysis, path, raw_expr)
        _walk_ast(node.index, analysis, path, raw_expr)
        return

    if isinstance(node, ListNode):
        for elem in node.elements:
            _walk_ast(elem, analysis, path, raw_expr)
        return

    if isinstance(node, UnaryNode):
        _walk_ast(node.operand, analysis, path, raw_expr)
        return

    if isinstance(node, BinaryNode):
        _walk_ast(node.left, analysis, path, raw_expr)
        _walk_ast(node.right, analysis, path, raw_expr)
        return

    if isinstance(node, TernaryNode):
        _walk_ast(node.condition, analysis, path, raw_expr)
        _walk_ast(node.when_true, analysis, path, raw_expr)
        _walk_ast(node.when_false, analysis, path, raw_expr)
        return

    if isinstance(node, PipeNode):
        if node.filter_name not in SAFE_FILTERS:
            analysis.issues.append(
                ExpressionIssue(
                    path=path,
                    expression=raw_expr,
                    message=f"Unknown or disallowed filter '{node.filter_name}'.",
                )
            )
        _walk_ast(node.target, analysis, path, raw_expr)
        for arg in node.args:
            _walk_ast(arg, analysis, path, raw_expr)


def inspect_expression(raw_expr: str, *, path: str = "$") -> ExpressionAnalysis:
    """Parse and statically analyze a single raw expression (with or without {{ }})."""
    analysis = ExpressionAnalysis()
    text = raw_expr.strip()
    if text.startswith("{{") and text.endswith("}}"):
        inner = text[2:-2]
        if "{{" not in inner and "}}" not in inner:
            text = inner.strip()
    analysis.expressions.append(text)
    try:
        ast = parse_expression(text)
    except ExpressionError as exc:
        analysis.issues.append(ExpressionIssue(path=path, expression=raw_expr, message=str(exc)))
        return analysis
    _walk_ast(ast, analysis, path, raw_expr)
    return analysis


def inspect_value(value: Any, *, path: str = "$") -> ExpressionAnalysis:
    """Recursively find and statically analyze all {{ ... }} expressions in a JSON-like value."""
    analysis = ExpressionAnalysis()
    _inspect_recursive(value, path, analysis)
    return analysis


def _inspect_recursive(value: Any, path: str, analysis: ExpressionAnalysis) -> None:
    if isinstance(value, str):
        # Check for unbalanced {{ or }} delimiters
        open_count = value.count("{{")
        close_count = value.count("}}")
        if open_count != close_count:
            analysis.issues.append(
                ExpressionIssue(
                    path=path,
                    expression=value,
                    message="Unbalanced expression delimiters '{{' and '}}'.",
                )
            )
            return

        for match in TEMPLATE_PATTERN.finditer(value):
            expr_text = match.group(1).strip()
            analysis.expressions.append(expr_text)
            try:
                ast = parse_expression(expr_text)
            except ExpressionError as exc:
                analysis.issues.append(
                    ExpressionIssue(path=path, expression=expr_text, message=str(exc))
                )
                continue
            _walk_ast(ast, analysis, path, expr_text)
        return

    if isinstance(value, list):
        for idx, item in enumerate(value):
            _inspect_recursive(item, f"{path}[{idx}]", analysis)
        return

    if isinstance(value, dict):
        for key, item in value.items():
            _inspect_recursive(item, f"{path}.{key}", analysis)
