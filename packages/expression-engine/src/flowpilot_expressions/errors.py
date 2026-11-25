"""Exceptions raised by the FlowPilot restricted expression engine."""

from __future__ import annotations


class ExpressionError(Exception):
    """Base error for expression parsing, safety, or evaluation failures."""


class ExpressionSyntaxError(ExpressionError):
    """Raised when an expression cannot be lexed or parsed."""


class ExpressionSecurityError(ExpressionError):
    """Raised when an expression violates sandbox safety rules."""


class ExpressionEvaluationError(ExpressionError):
    """Raised when evaluating a valid expression fails at runtime."""
