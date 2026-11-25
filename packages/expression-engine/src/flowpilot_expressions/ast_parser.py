"""Safe lexer and recursive-descent parser for FlowPilot expressions.

Never uses eval(), exec(), or Python's ast/bytecode execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flowpilot_expressions.errors import ExpressionSecurityError, ExpressionSyntaxError

MAX_EXPRESSION_LENGTH = 4096
MAX_AST_DEPTH = 32


# --- AST Nodes ---


class ExprNode:
    """Base class for all expression AST nodes."""


@dataclass(frozen=True, slots=True)
class LiteralNode(ExprNode):
    value: Any


@dataclass(frozen=True, slots=True)
class IdentifierNode(ExprNode):
    name: str


@dataclass(frozen=True, slots=True)
class ListNode(ExprNode):
    elements: tuple[ExprNode, ...]


@dataclass(frozen=True, slots=True)
class AttrNode(ExprNode):
    target: ExprNode
    attribute: str


@dataclass(frozen=True, slots=True)
class IndexNode(ExprNode):
    target: ExprNode
    index: ExprNode


@dataclass(frozen=True, slots=True)
class UnaryNode(ExprNode):
    operator: str
    operand: ExprNode


@dataclass(frozen=True, slots=True)
class BinaryNode(ExprNode):
    operator: str
    left: ExprNode
    right: ExprNode


@dataclass(frozen=True, slots=True)
class TernaryNode(ExprNode):
    condition: ExprNode
    when_true: ExprNode
    when_false: ExprNode


@dataclass(frozen=True, slots=True)
class PipeNode(ExprNode):
    target: ExprNode
    filter_name: str
    args: tuple[ExprNode, ...]


# --- Lexer ---


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: Any
    pos: int


_TWO_CHAR_OPS = {
    "==": "EQ",
    "!=": "NEQ",
    "<=": "LTE",
    ">=": "GTE",
    "&&": "AND",
    "||": "OR",
    "??": "COALESCE",
}

_SINGLE_CHAR_TOKENS = {
    ".": "DOT",
    ",": "COMMA",
    "(": "LPAREN",
    ")": "RPAREN",
    "[": "LBRACKET",
    "]": "RBRACKET",
    "+": "PLUS",
    "-": "MINUS",
    "*": "STAR",
    "/": "SLASH",
    "%": "PERCENT",
    "<": "LT",
    ">": "GT",
    "!": "NOT",
    "?": "QUESTION",
    ":": "COLON",
    "|": "PIPE",
}

_KEYWORDS = {
    "true": ("BOOL", True),
    "false": ("BOOL", False),
    "null": ("NULL", None),
    "and": ("AND", "and"),
    "or": ("OR", "or"),
    "not": ("NOT", "not"),
    "in": ("IN", "in"),
    "contains": ("CONTAINS", "contains"),
}

_STRING_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    "'": "'",
    '"': '"',
}


def tokenize(source: str) -> list[Token]:
    """Convert expression text into a flat token list."""
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionSecurityError(
            f"Expression exceeds maximum allowed length of {MAX_EXPRESSION_LENGTH} characters."
        )

    tokens: list[Token] = []
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if ch.isspace():
            i += 1
            continue

        # Two-character operators
        if i + 1 < n and source[i : i + 2] in _TWO_CHAR_OPS:
            op = source[i : i + 2]
            tokens.append(Token(_TWO_CHAR_OPS[op], op, i))
            i += 2
            continue

        # Single-character tokens
        if ch in _SINGLE_CHAR_TOKENS:
            tokens.append(Token(_SINGLE_CHAR_TOKENS[ch], ch, i))
            i += 1
            continue

        # String literal
        if ch in ("'", '"'):
            quote = ch
            start = i
            i += 1
            chars: list[str] = []
            closed = False
            while i < n:
                c = source[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise ExpressionSyntaxError(f"Unterminated escape sequence at pos {i}.")
                    esc = source[i + 1]
                    chars.append(_STRING_ESCAPES.get(esc, esc))
                    i += 2
                    continue
                if c == quote:
                    closed = True
                    i += 1
                    break
                chars.append(c)
                i += 1
            if not closed:
                raise ExpressionSyntaxError(f"Unterminated string literal starting at pos {start}.")
            tokens.append(Token("STRING", "".join(chars), start))
            continue

        # Number literal (int or float)
        if ch.isdigit():
            start = i
            has_dot = False
            while i < n and (source[i].isdigit() or (source[i] == "." and not has_dot)):
                if source[i] == ".":
                    if i + 1 < n and source[i + 1].isdigit():
                        has_dot = True
                    else:
                        break
                i += 1
            raw = source[start:i]
            value: int | float = float(raw) if has_dot else int(raw)
            tokens.append(Token("NUMBER", value, start))
            continue

        # Identifiers and keywords
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (source[i].isalnum() or source[i] in ("_", "-")):
                # Allow hyphens inside node IDs after a dot or inside identifiers?
                # Keep '-' as subtraction operator unless preceded by DOT!
                if source[i] == "-":
                    if tokens and tokens[-1].kind == "DOT":
                        i += 1
                        continue
                    break
                i += 1
            ident = source[start:i]
            if ident.startswith("_"):
                raise ExpressionSecurityError(
                    f"Access to private/dunder identifier '{ident}' is forbidden."
                )
            kw = _KEYWORDS.get(ident)
            if kw is not None and not (tokens and tokens[-1].kind == "DOT"):
                tokens.append(Token(kw[0], kw[1], start))
            else:
                tokens.append(Token("IDENT", ident, start))
            continue

        raise ExpressionSyntaxError(f"Unexpected character {ch!r} at position {i}.")

    tokens.append(Token("EOF", None, n))
    return tokens


# --- Recursive-Descent Parser ---


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._depth = 0

    def _peek(self, offset: int = 0) -> Token:
        idx = self._pos + offset
        if idx >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[idx]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, kind: str) -> Token:
        tok = self._peek()
        if tok.kind != kind:
            raise ExpressionSyntaxError(
                f"Expected {kind} but got {tok.kind} ({tok.value!r}) at pos {tok.pos}."
            )
        return self._advance()

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > MAX_AST_DEPTH:
            raise ExpressionSecurityError(
                f"Expression nesting depth exceeds maximum allowed ({MAX_AST_DEPTH})."
            )

    def _leave(self) -> None:
        self._depth -= 1

    def parse(self) -> ExprNode:
        node = self._parse_pipe()
        if self._peek().kind != "EOF":
            tok = self._peek()
            raise ExpressionSyntaxError(
                f"Unexpected token {tok.value!r} at pos {tok.pos}."
            )
        return node

    def _parse_pipe(self) -> ExprNode:
        node = self._parse_ternary()
        while self._peek().kind == "PIPE":
            self._advance()
            filter_tok = self._expect("IDENT")
            args: list[ExprNode] = []
            if self._peek().kind == "LPAREN":
                self._advance()
                if self._peek().kind != "RPAREN":
                    args.append(self._parse_ternary())
                    while self._peek().kind == "COMMA":
                        self._advance()
                        args.append(self._parse_ternary())
                self._expect("RPAREN")
            node = PipeNode(target=node, filter_name=str(filter_tok.value), args=tuple(args))
        return node

    def _parse_ternary(self) -> ExprNode:
        self._enter()
        try:
            node = self._parse_coalesce()
            if self._peek().kind == "QUESTION":
                self._advance()
                when_true = self._parse_ternary()
                self._expect("COLON")
                when_false = self._parse_ternary()
                return TernaryNode(condition=node, when_true=when_true, when_false=when_false)
            return node
        finally:
            self._leave()

    def _parse_coalesce(self) -> ExprNode:
        node = self._parse_or()
        while self._peek().kind == "COALESCE":
            self._advance()
            right = self._parse_or()
            node = BinaryNode("??", node, right)
        return node

    def _parse_or(self) -> ExprNode:
        node = self._parse_and()
        while self._peek().kind == "OR":
            self._advance()
            right = self._parse_and()
            node = BinaryNode("or", node, right)
        return node

    def _parse_and(self) -> ExprNode:
        node = self._parse_equality()
        while self._peek().kind == "AND":
            self._advance()
            right = self._parse_equality()
            node = BinaryNode("and", node, right)
        return node

    def _parse_equality(self) -> ExprNode:
        node = self._parse_comparison()
        while self._peek().kind in ("EQ", "NEQ"):
            op = self._advance().value
            right = self._parse_comparison()
            node = BinaryNode(str(op), node, right)
        return node

    def _parse_comparison(self) -> ExprNode:
        node = self._parse_additive()
        while True:
            tok = self._peek()
            if tok.kind in ("LT", "LTE", "GT", "GTE", "IN", "CONTAINS"):
                op = self._advance().value
                right = self._parse_additive()
                node = BinaryNode(str(op), node, right)
            elif tok.kind == "NOT" and self._peek(1).kind == "IN":
                self._advance()
                self._advance()
                right = self._parse_additive()
                node = BinaryNode("not in", node, right)
            else:
                break
        return node

    def _parse_additive(self) -> ExprNode:
        node = self._parse_multiplicative()
        while self._peek().kind in ("PLUS", "MINUS"):
            op = self._advance().value
            right = self._parse_multiplicative()
            node = BinaryNode(str(op), node, right)
        return node

    def _parse_multiplicative(self) -> ExprNode:
        node = self._parse_unary()
        while self._peek().kind in ("STAR", "SLASH", "PERCENT"):
            op = self._advance().value
            right = self._parse_unary()
            node = BinaryNode(str(op), node, right)
        return node

    def _parse_unary(self) -> ExprNode:
        self._enter()
        try:
            tok = self._peek()
            if tok.kind == "NOT":
                self._advance()
                return UnaryNode("not", self._parse_unary())
            if tok.kind == "MINUS":
                self._advance()
                return UnaryNode("-", self._parse_unary())
            return self._parse_postfix()
        finally:
            self._leave()

    def _parse_postfix(self) -> ExprNode:
        node = self._parse_primary()
        while True:
            tok = self._peek()
            if tok.kind == "DOT":
                self._advance()
                attr_tok = self._expect("IDENT")
                attr = str(attr_tok.value)
                if attr.startswith("_"):
                    raise ExpressionSecurityError(
                        f"Access to private/dunder attribute '{attr}' is forbidden."
                    )
                node = AttrNode(target=node, attribute=attr)
            elif tok.kind == "LBRACKET":
                self._advance()
                idx_expr = self._parse_pipe()
                self._expect("RBRACKET")
                node = IndexNode(target=node, index=idx_expr)
            elif tok.kind == "LPAREN":
                raise ExpressionSecurityError(
                    "Direct function/method calls are forbidden; use built-in pipe filters instead."
                )
            else:
                break
        return node

    def _parse_primary(self) -> ExprNode:
        tok = self._peek()
        if tok.kind in ("NUMBER", "STRING", "BOOL", "NULL"):
            self._advance()
            return LiteralNode(tok.value)
        if tok.kind == "IDENT":
            self._advance()
            return IdentifierNode(str(tok.value))
        if tok.kind == "LPAREN":
            self._advance()
            expr = self._parse_pipe()
            self._expect("RPAREN")
            return expr
        if tok.kind == "LBRACKET":
            self._advance()
            elements: list[ExprNode] = []
            if self._peek().kind != "RBRACKET":
                elements.append(self._parse_pipe())
                while self._peek().kind == "COMMA":
                    self._advance()
                    elements.append(self._parse_pipe())
            self._expect("RBRACKET")
            return ListNode(tuple(elements))
        raise ExpressionSyntaxError(
            f"Unexpected token {tok.kind} ({tok.value!r}) at pos {tok.pos}."
        )


def parse_expression(source: str) -> ExprNode:
    """Parse a raw expression string (without surrounding {{ }}) into a safe AST."""
    stripped = source.strip()
    if not stripped:
        raise ExpressionSyntaxError("Empty expression is not allowed.")
    tokens = tokenize(stripped)
    return _Parser(tokens).parse()
