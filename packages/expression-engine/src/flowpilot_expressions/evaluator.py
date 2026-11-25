"""Deterministic sandboxed evaluator and template resolver for FlowPilot expressions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
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
from flowpilot_expressions.errors import ExpressionEvaluationError, ExpressionSecurityError

TEMPLATE_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
ALLOWED_ROOTS = frozenset({"nodes", "trigger", "inputs", "env", "loop", "run", "error"})
MAX_EVAL_STEPS = 500


def _filter_lower(value: Any) -> str:
    return "" if value is None else str(value).lower()


def _filter_upper(value: Any) -> str:
    return "" if value is None else str(value).upper()


def _filter_trim(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _filter_length(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (str, list, tuple, dict, set)):
        return len(value)
    raise ExpressionEvaluationError(f"Cannot compute length of {type(value).__name__}.")


def _filter_string(value: Any) -> str:
    return "" if value is None else str(value)


def _filter_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ExpressionEvaluationError(f"Cannot convert {value!r} to int.") from exc


def _filter_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ExpressionEvaluationError(f"Cannot convert {value!r} to float.") from exc


def _filter_bool(value: Any) -> bool:
    return bool(value)


def _filter_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _filter_keys(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [str(k) for k in value]
    raise ExpressionEvaluationError("Filter 'keys' requires a mapping/object.")


def _filter_values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    raise ExpressionEvaluationError("Filter 'values' requires a mapping/object.")


def _filter_first(value: Any) -> Any:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value[0] if len(value) > 0 else None
    raise ExpressionEvaluationError("Filter 'first' requires a list.")


def _filter_last(value: Any) -> Any:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value[-1] if len(value) > 0 else None
    raise ExpressionEvaluationError("Filter 'last' requires a list.")


def _filter_default(value: Any, fallback: Any = "") -> Any:
    return fallback if value is None or value == "" else value


def _filter_round(value: Any, ndigits: int = 0) -> float | int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ExpressionEvaluationError("Filter 'round' requires a numeric value.")
    return round(value, int(ndigits))


def _filter_join(value: Any, sep: str = ",") -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return str(sep).join("" if item is None else str(item) for item in value)
    raise ExpressionEvaluationError("Filter 'join' requires a list.")


def _filter_split(value: Any, sep: str = ",") -> list[str]:
    if value is None:
        return []
    return str(value).split(str(sep))


def _filter_replace(value: Any, old: str, new: str) -> str:
    if value is None:
        return ""
    return str(value).replace(str(old), str(new))


def _filter_slice(value: Any, start: int = 0, end: int | None = None) -> Any:
    if isinstance(value, (str, list, tuple)):
        return value[int(start) : (None if end is None else int(end))]
    raise ExpressionEvaluationError("Filter 'slice' requires a string or list.")


SAFE_FILTERS: dict[str, Callable[..., Any]] = {
    "lower": _filter_lower,
    "upper": _filter_upper,
    "trim": _filter_trim,
    "length": _filter_length,
    "string": _filter_string,
    "int": _filter_int,
    "float": _filter_float,
    "bool": _filter_bool,
    "json": _filter_json,
    "keys": _filter_keys,
    "values": _filter_values,
    "first": _filter_first,
    "last": _filter_last,
    "default": _filter_default,
    "round": _filter_round,
    "join": _filter_join,
    "split": _filter_split,
    "replace": _filter_replace,
    "slice": _filter_slice,
}


class _Evaluator:
    def __init__(
        self,
        context: Mapping[str, Any],
        *,
        allowed_roots: frozenset[str] = ALLOWED_ROOTS,
        strict_missing: bool = False,
        max_steps: int = MAX_EVAL_STEPS,
    ) -> None:
        self._context = context
        self._allowed_roots = allowed_roots
        self._strict_missing = strict_missing
        self._max_steps = max_steps
        self._steps = 0

    def _tick(self) -> None:
        self._steps += 1
        if self._steps > self._max_steps:
            raise ExpressionSecurityError(
                f"Expression evaluation exceeded step limit ({self._max_steps})."
            )

    def eval(self, node: ExprNode) -> Any:
        self._tick()

        if isinstance(node, LiteralNode):
            return node.value

        if isinstance(node, IdentifierNode):
            if node.name not in self._allowed_roots:
                raise ExpressionSecurityError(
                    f"Root identifier '{node.name}' is not allowed. "
                    f"Permitted roots: {sorted(self._allowed_roots)}."
                )
            if node.name not in self._context:
                if self._strict_missing:
                    raise ExpressionEvaluationError(f"Root '{node.name}' is missing in context.")
                return None
            return self._context[node.name]

        if isinstance(node, ListNode):
            return [self.eval(elem) for elem in node.elements]

        if isinstance(node, AttrNode):
            if node.attribute.startswith("_"):
                raise ExpressionSecurityError(
                    f"Access to private/dunder attribute '{node.attribute}' is forbidden."
                )
            target = self.eval(node.target)
            if target is None:
                if self._strict_missing:
                    raise ExpressionEvaluationError(
                        f"Cannot access attribute '{node.attribute}' on null."
                    )
                return None
            if isinstance(target, Mapping):
                if node.attribute in target:
                    return target[node.attribute]
                if self._strict_missing:
                    raise ExpressionEvaluationError(f"Key '{node.attribute}' not found.")
                return None
            raise ExpressionSecurityError(
                f"Attribute access '{node.attribute}' is only allowed on JSON objects/dicts, "
                f"not {type(target).__name__}."
            )

        if isinstance(node, IndexNode):
            target = self.eval(node.target)
            index = self.eval(node.index)
            if target is None:
                return None
            if isinstance(target, Mapping):
                if isinstance(index, str) and index.startswith("_"):
                    raise ExpressionSecurityError(
                        f"Access to private/dunder key '{index}' is forbidden."
                    )
                return target.get(index)
            if isinstance(target, Sequence) and not isinstance(target, (str, bytes)):
                if not isinstance(index, int) or isinstance(index, bool):
                    raise ExpressionEvaluationError(
                        f"List index must be an integer, got {type(index).__name__}."
                    )
                if -len(target) <= index < len(target):
                    return target[index]
                if self._strict_missing:
                    raise ExpressionEvaluationError(f"List index {index} out of range.")
                return None
            raise ExpressionEvaluationError(
                f"Cannot index into value of type {type(target).__name__}."
            )

        if isinstance(node, UnaryNode):
            val = self.eval(node.operand)
            if node.operator == "not":
                return not bool(val)
            if node.operator == "-":
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return -val
                raise ExpressionEvaluationError(f"Unary '-' requires a number, got {val!r}.")
            raise ExpressionEvaluationError(f"Unsupported unary operator '{node.operator}'.")

        if isinstance(node, BinaryNode):
            return self._eval_binary(node)

        if isinstance(node, TernaryNode):
            return self.eval(node.when_true) if bool(self.eval(node.condition)) else self.eval(
                node.when_false
            )

        if isinstance(node, PipeNode):
            fn = SAFE_FILTERS.get(node.filter_name)
            if fn is None:
                raise ExpressionSecurityError(
                    f"Unknown or disallowed filter '{node.filter_name}'."
                )
            val = self.eval(node.target)
            args = [self.eval(a) for a in node.args]
            try:
                return fn(val, *args)
            except TypeError as exc:
                raise ExpressionEvaluationError(
                    f"Invalid arguments for filter '{node.filter_name}': {exc}"
                ) from exc

        raise ExpressionEvaluationError(f"Unsupported AST node: {type(node).__name__}")

    def _eval_binary(self, node: BinaryNode) -> Any:
        op = node.operator
        if op == "and":
            left = self.eval(node.left)
            return left if not bool(left) else self.eval(node.right)
        if op == "or":
            left = self.eval(node.left)
            return left if bool(left) else self.eval(node.right)
        if op == "??":
            left = self.eval(node.left)
            return left if left is not None else self.eval(node.right)

        left = self.eval(node.left)
        right = self.eval(node.right)

        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return _compare(left, right, lambda a, b: a < b)
        if op == "<=":
            return _compare(left, right, lambda a, b: a <= b)
        if op == ">":
            return _compare(left, right, lambda a, b: a > b)
        if op == ">=":
            return _compare(left, right, lambda a, b: a >= b)
        if op == "in":
            return False if right is None else left in right
        if op == "not in":
            return True if right is None else left not in right
        if op == "contains":
            return False if left is None else right in left
        if op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return f"{'' if left is None else left}{'' if right is None else right}"
            return _numeric_op(left, right, lambda a, b: a + b, "+")
        if op == "-":
            return _numeric_op(left, right, lambda a, b: a - b, "-")
        if op == "*":
            return _numeric_op(left, right, lambda a, b: a * b, "*")
        if op == "/":
            if right == 0:
                raise ExpressionEvaluationError("Division by zero.")
            return _numeric_op(left, right, lambda a, b: a / b, "/")
        if op == "%":
            if right == 0:
                raise ExpressionEvaluationError("Modulo by zero.")
            return _numeric_op(left, right, lambda a, b: a % b, "%")

        raise ExpressionEvaluationError(f"Unsupported binary operator '{op}'.")


def _compare(left: Any, right: Any, op: Callable[[Any, Any], bool]) -> bool:
    try:
        return bool(op(left, right))
    except TypeError as exc:
        raise ExpressionEvaluationError(
            f"Cannot compare {type(left).__name__} and {type(right).__name__}."
        ) from exc


def _numeric_op(
    left: Any,
    right: Any,
    op: Callable[[int | float, int | float], int | float],
    symbol: str,
) -> int | float:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return op(left, right)
    raise ExpressionEvaluationError(
        f"Operator '{symbol}' requires numeric operands, got "
        f"{type(left).__name__} and {type(right).__name__}."
    )


def evaluate_expression(
    expression: str,
    context: Mapping[str, Any],
    *,
    strict_missing: bool = False,
    allowed_roots: frozenset[str] = ALLOWED_ROOTS,
) -> Any:
    """Evaluate a raw expression (with or without surrounding {{ }}) deterministically."""
    text = expression.strip()
    if text.startswith("{{") and text.endswith("}}"):
        inner = text[2:-2]
        if "{{" not in inner and "}}" not in inner:
            text = inner.strip()
    ast = parse_expression(text)
    return _Evaluator(
        context,
        allowed_roots=allowed_roots,
        strict_missing=strict_missing,
    ).eval(ast)


def resolve_template(
    template: str,
    context: Mapping[str, Any],
    *,
    strict_missing: bool = False,
    allowed_roots: frozenset[str] = ALLOWED_ROOTS,
) -> Any:
    """Resolve a template string containing zero, one, or many {{ expr }} blocks.

    If the entire string is a single '{{ expr }}', returns the native evaluated type.
    Otherwise interpolates each block into a string.
    """
    matches = list(TEMPLATE_PATTERN.finditer(template))
    if not matches:
        return template

    # Preserve native type when the entire string is a single expression
    if len(matches) == 1 and matches[0].start() == 0 and matches[0].end() == len(template):
        return evaluate_expression(
            matches[0].group(1),
            context,
            strict_missing=strict_missing,
            allowed_roots=allowed_roots,
        )

    parts: list[str] = []
    cursor = 0
    for match in matches:
        parts.append(template[cursor : match.start()])
        val = evaluate_expression(
            match.group(1),
            context,
            strict_missing=strict_missing,
            allowed_roots=allowed_roots,
        )
        if val is None:
            parts.append("")
        elif isinstance(val, (dict, list)):
            parts.append(json.dumps(val, sort_keys=True, ensure_ascii=False))
        else:
            parts.append(str(val))
        cursor = match.end()
    parts.append(template[cursor:])
    return "".join(parts)


def resolve_value(
    value: Any,
    context: Mapping[str, Any],
    *,
    strict_missing: bool = False,
    allowed_roots: frozenset[str] = ALLOWED_ROOTS,
) -> Any:
    """Recursively resolve all template expressions inside nested dicts, lists, and strings."""
    if isinstance(value, str):
        return resolve_template(
            value,
            context,
            strict_missing=strict_missing,
            allowed_roots=allowed_roots,
        )
    if isinstance(value, list):
        return [
            resolve_value(
                item,
                context,
                strict_missing=strict_missing,
                allowed_roots=allowed_roots,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: resolve_value(
                val,
                context,
                strict_missing=strict_missing,
                allowed_roots=allowed_roots,
            )
            for key, val in value.items()
        }
    return value
