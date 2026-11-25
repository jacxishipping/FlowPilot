"""Comprehensive pre-publish validator for canonical FlowPilot workflows."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any

from pydantic import ValidationError

from flowpilot import BaseNode, NodeRegistry
from flowpilot_expressions import (
    TEMPLATE_PATTERN,
    inspect_expression,
    inspect_value,
)
from flowpilot_schema import (
    EdgeKind,
    NodeDefinition,
    NodeKind,
    SideEffectPolicy,
    WorkflowDefinition,
    WorkflowGraph,
)
from flowpilot_validator.models import Severity, ValidationIssue, ValidationResult

MAX_LOOP_NESTING_DEPTH = 3

# Heuristic patterns for catching accidentally hardcoded plaintext secrets in workflow JSON
_PLAINTEXT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI/Stripe-style secret key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("GitHub personal access token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack bot/user token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Hardcoded Bearer token", re.compile(r"(?i)^Bearer\s+[A-Za-z0-9._\-]{20,}$")),
)


class WorkflowValidator:
    """Validates structure, acyclicity, loops, expressions, schemas, credentials, and policies."""

    def __init__(
        self,
        registry: NodeRegistry,
        *,
        max_loop_depth: int = MAX_LOOP_NESTING_DEPTH,
    ) -> None:
        self._registry = registry
        self._max_loop_depth = max_loop_depth

    def validate(
        self,
        definition: WorkflowDefinition,
        *,
        granted_permissions: set[str] | None = None,
    ) -> ValidationResult:
        """Run all pre-publish validation checks against a canonical WorkflowDefinition."""
        issues: list[ValidationIssue] = []

        self._validate_graph(
            definition.graph,
            issues=issues,
            is_root=True,
            loop_depth=0,
            outer_ancestors=set(),
            granted_permissions=granted_permissions,
            path_prefix="graph",
        )

        # Validate top-level output_mapping expressions against all root graph nodes
        if definition.output_mapping:
            root_node_ids = {n.id for n in definition.graph.nodes}
            analysis = inspect_value(definition.output_mapping, path="output_mapping")
            for expr_issue in analysis.issues:
                issues.append(
                    ValidationIssue(
                        code="INVALID_EXPRESSION",
                        severity=Severity.ERROR,
                        message=expr_issue.message,
                        path=expr_issue.path,
                    )
                )
            for ref in sorted(analysis.node_refs):
                if ref not in root_node_ids:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_NODE_REFERENCE",
                            severity=Severity.ERROR,
                            message=(
                                f"Workflow output_mapping references unknown node '{ref}'."
                            ),
                            path="output_mapping",
                        )
                    )

        return ValidationResult(issues=issues)

    def _validate_graph(
        self,
        graph: WorkflowGraph,
        *,
        issues: list[ValidationIssue],
        is_root: bool,
        loop_depth: int,
        outer_ancestors: set[str],
        granted_permissions: set[str] | None,
        path_prefix: str,
    ) -> None:
        if not graph.nodes:
            issues.append(
                ValidationIssue(
                    code="EMPTY_GRAPH",
                    severity=Severity.ERROR,
                    message="Workflow graph must contain at least one node.",
                    path=path_prefix,
                )
            )
            return

        # 1. Node uniqueness & node-type lookup
        nodes_by_id: dict[str, NodeDefinition] = {}
        node_classes: dict[str, type[BaseNode[Any, Any]]] = {}

        for node in graph.nodes:
            if node.id in nodes_by_id:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_NODE_ID",
                        severity=Severity.ERROR,
                        message=f"Duplicate node id '{node.id}'.",
                        node_id=node.id,
                        path=f"{path_prefix}.nodes.{node.id}",
                    )
                )
                continue
            nodes_by_id[node.id] = node

            node_cls = self._registry.get(node.type)
            if node_cls is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_NODE_TYPE",
                        severity=Severity.ERROR,
                        message=f"Node type '{node.type}' is not registered.",
                        node_id=node.id,
                        path=f"{path_prefix}.nodes.{node.id}.type",
                    )
                )
            else:
                node_classes[node.id] = node_cls

        # 2. Edge structural integrity
        seen_edge_ids: set[str] = set()
        seen_edge_tuples: set[tuple[str, str, EdgeKind]] = set()
        outgoing: dict[str, list[str]] = defaultdict(list)
        incoming: dict[str, list[str]] = defaultdict(list)

        for edge in graph.edges:
            if edge.id in seen_edge_ids:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_EDGE_ID",
                        severity=Severity.ERROR,
                        message=f"Duplicate edge id '{edge.id}'.",
                        edge_id=edge.id,
                        path=f"{path_prefix}.edges.{edge.id}",
                    )
                )
            seen_edge_ids.add(edge.id)

            valid_endpoints = True
            if edge.source not in nodes_by_id:
                issues.append(
                    ValidationIssue(
                        code="EDGE_UNKNOWN_SOURCE",
                        severity=Severity.ERROR,
                        message=f"Edge '{edge.id}' references unknown source node '{edge.source}'.",
                        edge_id=edge.id,
                    )
                )
                valid_endpoints = False
            if edge.target not in nodes_by_id:
                issues.append(
                    ValidationIssue(
                        code="EDGE_UNKNOWN_TARGET",
                        severity=Severity.ERROR,
                        message=f"Edge '{edge.id}' references unknown target node '{edge.target}'.",
                        edge_id=edge.id,
                    )
                )
                valid_endpoints = False

            if edge.source == edge.target and valid_endpoints:
                issues.append(
                    ValidationIssue(
                        code="SELF_LOOP_EDGE",
                        severity=Severity.ERROR,
                        message=f"Edge '{edge.id}' connects node '{edge.source}' to itself.",
                        node_id=edge.source,
                        edge_id=edge.id,
                    )
                )
                continue

            if not valid_endpoints:
                continue

            edge_key = (edge.source, edge.target, edge.kind)
            if edge_key in seen_edge_tuples:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_EDGE",
                        severity=Severity.WARNING,
                        message=(
                            f"Duplicate {edge.kind.value} edge from '{edge.source}' "
                            f"to '{edge.target}'."
                        ),
                        edge_id=edge.id,
                    )
                )
            seen_edge_tuples.add(edge_key)

            # Validate branch edge kinds against source node output_branches
            src_cls = node_classes.get(edge.source)
            if (
                src_cls is not None
                and edge.kind in (EdgeKind.TRUE, EdgeKind.FALSE)
                and edge.kind.value not in src_cls.metadata.output_branches
            ):
                issues.append(
                    ValidationIssue(
                        code="INVALID_EDGE_KIND",
                        severity=Severity.ERROR,
                        message=(
                            f"Node '{edge.source}' (type '{src_cls.metadata.type}') does not "
                            f"declare branch '{edge.kind.value}'."
                        ),
                        node_id=edge.source,
                        edge_id=edge.id,
                    )
                )

            outgoing[edge.source].append(edge.target)
            incoming[edge.target].append(edge.source)

        # 3. Entry points & trigger rules
        trigger_ids = [
            nid
            for nid, cls in node_classes.items()
            if cls.metadata.kind == NodeKind.TRIGGER
        ]
        if is_root:
            if not trigger_ids:
                issues.append(
                    ValidationIssue(
                        code="MISSING_ENTRY_POINT",
                        severity=Severity.ERROR,
                        message=(
                            "Root workflow graph must contain at least one trigger/entry node."
                        ),
                        path=path_prefix,
                    )
                )
            for tid in trigger_ids:
                if incoming[tid]:
                    issues.append(
                        ValidationIssue(
                            code="TRIGGER_HAS_INCOMING_EDGE",
                            severity=Severity.ERROR,
                            message=f"Trigger node '{tid}' cannot have incoming edges.",
                            node_id=tid,
                        )
                    )
            entry_ids = trigger_ids
        else:
            for tid in trigger_ids:
                issues.append(
                    ValidationIssue(
                        code="LOOP_BODY_CONTAINS_TRIGGER",
                        severity=Severity.ERROR,
                        message=(
                            f"Loop body cannot contain trigger node '{tid}'."
                        ),
                        node_id=tid,
                    )
                )
            entry_ids = [nid for nid in nodes_by_id if not incoming[nid]]

        # 4. Strict DAG acyclicity check (arbitrary cycles prohibited)
        cycle = _find_cycle(list(nodes_by_id.keys()), outgoing)
        if cycle is not None:
            issues.append(
                ValidationIssue(
                    code="GRAPH_CYCLE_DETECTED",
                    severity=Severity.ERROR,
                    message=(
                        "Arbitrary cycles are prohibited; use an explicit loop node instead. "
                        f"Cycle: {' -> '.join(cycle)}"
                    ),
                    path=path_prefix,
                    details={"cycle": cycle},
                )
            )
            # Still compute best-effort ancestors to report other issues cleanly
            ancestors_map: dict[str, set[str]] = {nid: set() for nid in nodes_by_id}
        else:
            ancestors_map = _compute_ancestors(list(nodes_by_id.keys()), outgoing, incoming)

        # 5. Unreachable nodes warning
        if entry_ids:
            reachable = _compute_reachable(entry_ids, outgoing)
            for nid in nodes_by_id:
                if nid not in reachable:
                    issues.append(
                        ValidationIssue(
                            code="UNREACHABLE_NODE",
                            severity=Severity.WARNING,
                            message=(
                                f"Node '{nid}' is not reachable from any entry point."
                            ),
                            node_id=nid,
                        )
                    )

        # 6. Per-node config schema, credentials, secrets, permissions, expressions, and loops
        for nid, node in nodes_by_id.items():
            node_cls = node_classes.get(nid)
            node_path = f"{path_prefix}.nodes.{nid}"

            # Plaintext secret check
            _scan_plaintext_secrets(
                node.config,
                node_id=nid,
                path=f"{node_path}.config",
                issues=issues,
            )

            # Expression syntax + upstream reachability check
            valid_upstream = outer_ancestors | ancestors_map.get(nid, set())
            self._validate_node_expressions(
                node=node,
                all_local_ids=set(nodes_by_id.keys()),
                valid_upstream=valid_upstream,
                issues=issues,
                node_path=node_path,
            )

            if node_cls is not None:
                self._validate_node_contract(
                    node=node,
                    node_cls=node_cls,
                    granted_permissions=granted_permissions,
                    issues=issues,
                    node_path=node_path,
                )

                # Explicit loop validation
                if node_cls.metadata.is_loop:
                    if node.loop is None:
                        issues.append(
                            ValidationIssue(
                                code="MISSING_LOOP_SPEC",
                                severity=Severity.ERROR,
                                message=f"Loop node '{nid}' must define a 'loop' specification.",
                                node_id=nid,
                                path=f"{node_path}.loop",
                            )
                        )
                    else:
                        self._validate_loop_node(
                            node=node,
                            valid_upstream=valid_upstream,
                            loop_depth=loop_depth,
                            granted_permissions=granted_permissions,
                            issues=issues,
                            node_path=node_path,
                        )
                elif node.loop is not None:
                    issues.append(
                        ValidationIssue(
                            code="UNEXPECTED_LOOP_SPEC",
                            severity=Severity.ERROR,
                            message=(
                                f"Non-loop node '{nid}' (type '{node.type}') cannot define 'loop'."
                            ),
                            node_id=nid,
                            path=f"{node_path}.loop",
                        )
                    )

    def _validate_node_contract(
        self,
        *,
        node: NodeDefinition,
        node_cls: type[BaseNode[Any, Any]],
        granted_permissions: set[str] | None,
        issues: list[ValidationIssue],
        node_path: str,
    ) -> None:
        # Config schema check (skipping dynamic expression fields until runtime)
        _validate_static_config(node, node_cls, issues=issues, node_path=node_path)

        # Credential declarations
        declared_slots = {req.slot: req for req in node_cls.metadata.credentials}
        for req in node_cls.metadata.credentials:
            if req.required and req.slot not in node.credentials:
                issues.append(
                    ValidationIssue(
                        code="MISSING_REQUIRED_CREDENTIAL",
                        severity=Severity.ERROR,
                        message=(
                            f"Node '{node.id}' requires credential slot '{req.slot}' "
                            f"(type '{req.credential_type}')."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.credentials.{req.slot}",
                    )
                )
        for bound_slot in node.credentials:
            if bound_slot not in declared_slots:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_CREDENTIAL_SLOT",
                        severity=Severity.WARNING,
                        message=(
                            f"Credential slot '{bound_slot}' is not declared by node type "
                            f"'{node.type}'."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.credentials.{bound_slot}",
                    )
                )

        # Permission checks
        if granted_permissions is not None:
            needed = set(node_cls.metadata.required_permissions) | set(
                node.policy.required_permissions
            )
            missing_perms = sorted(needed - granted_permissions)
            for perm in missing_perms:
                issues.append(
                    ValidationIssue(
                        code="MISSING_PERMISSION",
                        severity=Severity.ERROR,
                        message=(
                            f"Node '{node.id}' requires permission '{perm}' which is not granted."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.policy.required_permissions",
                    )
                )

        # Irreversible side-effect retry safety
        if node_cls.metadata.side_effect == SideEffectPolicy.IRREVERSIBLE:
            retry = node.policy.retry
            if retry.max_attempts > 1:
                if retry.allow_retry_with_idempotency_key:
                    if not node.policy.idempotency_key_expression:
                        issues.append(
                            ValidationIssue(
                                code="IRREVERSIBLE_RETRY_WITHOUT_IDEMPOTENCY_KEY",
                                severity=Severity.ERROR,
                                message=(
                                    f"Irreversible node '{node.id}' enables retries with "
                                    "idempotency key, but 'idempotency_key_expression' is missing."
                                ),
                                node_id=node.id,
                                path=f"{node_path}.policy.idempotency_key_expression",
                            )
                        )
                else:
                    issues.append(
                        ValidationIssue(
                            code="IRREVERSIBLE_NODE_RETRIES_IGNORED",
                            severity=Severity.WARNING,
                            message=(
                                f"Irreversible node '{node.id}' has max_attempts="
                                f"{retry.max_attempts}; retries are blocked unless "
                                "allow_retry_with_idempotency_key=True and "
                                "idempotency_key_expression is set."
                            ),
                            node_id=node.id,
                            path=f"{node_path}.policy.retry",
                        )
                    )

    def _validate_node_expressions(
        self,
        *,
        node: NodeDefinition,
        all_local_ids: set[str],
        valid_upstream: set[str],
        issues: list[ValidationIssue],
        node_path: str,
    ) -> None:
        analysis = inspect_value(node.config, path=f"{node_path}.config")
        if node.policy.idempotency_key_expression:
            idem_analysis = inspect_value(
                node.policy.idempotency_key_expression,
                path=f"{node_path}.policy.idempotency_key_expression",
            )
            analysis.issues.extend(idem_analysis.issues)
            analysis.node_refs.update(idem_analysis.node_refs)
            analysis.uses_dynamic_node_ref = (
                analysis.uses_dynamic_node_ref or idem_analysis.uses_dynamic_node_ref
            )

        for expr_issue in analysis.issues:
            issues.append(
                ValidationIssue(
                    code="INVALID_EXPRESSION",
                    severity=Severity.ERROR,
                    message=expr_issue.message,
                    node_id=node.id,
                    path=expr_issue.path,
                )
            )

        if analysis.uses_dynamic_node_ref:
            issues.append(
                ValidationIssue(
                    code="DYNAMIC_NODE_REFERENCE",
                    severity=Severity.WARNING,
                    message=(
                        f"Node '{node.id}' uses dynamic node lookup 'nodes[...]'; "
                        "upstream reachability cannot be statically guaranteed."
                    ),
                    node_id=node.id,
                    path=f"{node_path}.config",
                )
            )

        for ref_id in sorted(analysis.node_refs):
            if ref_id == node.id:
                issues.append(
                    ValidationIssue(
                        code="SELF_NODE_REFERENCE",
                        severity=Severity.ERROR,
                        message=f"Node '{node.id}' cannot reference its own output.",
                        node_id=node.id,
                        path=f"{node_path}.config",
                    )
                )
            elif ref_id not in all_local_ids and ref_id not in valid_upstream:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_NODE_REFERENCE",
                        severity=Severity.ERROR,
                        message=(
                            f"Node '{node.id}' references unknown node '{ref_id}'."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.config",
                    )
                )
            elif ref_id not in valid_upstream:
                issues.append(
                    ValidationIssue(
                        code="NON_UPSTREAM_NODE_REFERENCE",
                        severity=Severity.ERROR,
                        message=(
                            f"Node '{node.id}' references node '{ref_id}', which is not an "
                            "upstream ancestor in the DAG."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.config",
                    )
                )

    def _validate_loop_node(
        self,
        *,
        node: NodeDefinition,
        valid_upstream: set[str],
        loop_depth: int,
        granted_permissions: set[str] | None,
        issues: list[ValidationIssue],
        node_path: str,
    ) -> None:
        assert node.loop is not None
        next_depth = loop_depth + 1
        if next_depth > self._max_loop_depth:
            issues.append(
                ValidationIssue(
                    code="LOOP_NESTING_TOO_DEEP",
                    severity=Severity.ERROR,
                    message=(
                        f"Loop node '{node.id}' exceeds maximum allowed loop nesting depth "
                        f"({self._max_loop_depth})."
                    ),
                    node_id=node.id,
                    path=f"{node_path}.loop",
                )
            )
            return

        if node.loop.max_iterations > 1_000:
            issues.append(
                ValidationIssue(
                    code="HIGH_LOOP_ITERATION_LIMIT",
                    severity=Severity.WARNING,
                    message=(
                        f"Loop node '{node.id}' allows up to {node.loop.max_iterations} "
                        "iterations; verify budget and timeout settings."
                    ),
                    node_id=node.id,
                    path=f"{node_path}.loop.max_iterations",
                )
            )

        # Validate items_expression
        items_analysis = inspect_expression(
            node.loop.items_expression,
            path=f"{node_path}.loop.items_expression",
        )
        for expr_issue in items_analysis.issues:
            issues.append(
                ValidationIssue(
                    code="INVALID_EXPRESSION",
                    severity=Severity.ERROR,
                    message=expr_issue.message,
                    node_id=node.id,
                    path=expr_issue.path,
                )
            )
        for ref_id in sorted(items_analysis.node_refs):
            if ref_id not in valid_upstream:
                issues.append(
                    ValidationIssue(
                        code="NON_UPSTREAM_NODE_REFERENCE",
                        severity=Severity.ERROR,
                        message=(
                            f"Loop '{node.id}' items_expression references '{ref_id}', "
                            "which is not an upstream ancestor."
                        ),
                        node_id=node.id,
                        path=f"{node_path}.loop.items_expression",
                    )
                )

        # Recursively validate loop body_graph with outer_ancestors visible
        self._validate_graph(
            node.loop.body_graph,
            issues=issues,
            is_root=False,
            loop_depth=next_depth,
            outer_ancestors=valid_upstream,
            granted_permissions=granted_permissions,
            path_prefix=f"{node_path}.loop.body_graph",
        )


# --- Graph & Config Helpers ---


def _find_cycle(node_ids: list[str], outgoing: dict[str, list[str]]) -> list[str] | None:
    """DFS cycle detector returning the exact cycle path if any cycle exists."""
    state: dict[str, int] = {nid: 0 for nid in node_ids}  # 0=unvisited, 1=visiting, 2=visited
    path: list[str] = []

    def dfs(u: str) -> list[str] | None:
        state[u] = 1
        path.append(u)
        for v in outgoing.get(u, []):
            if state[v] == 1:
                idx = path.index(v)
                return [*path[idx:], v]
            if state[v] == 0:
                found = dfs(v)
                if found is not None:
                    return found
        path.pop()
        state[u] = 2
        return None

    for nid in node_ids:
        if state[nid] == 0:
            found = dfs(nid)
            if found is not None:
                return found
    return None


def _compute_ancestors(
    node_ids: list[str],
    outgoing: dict[str, list[str]],
    incoming: dict[str, list[str]],
) -> dict[str, set[str]]:
    """Compute strict ancestor set for every node in an acyclic graph using Kahn's order."""
    in_degree = {nid: len(incoming.get(nid, [])) for nid in node_ids}
    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    ancestors: dict[str, set[str]] = {nid: set() for nid in node_ids}

    while queue:
        u = queue.popleft()
        for v in outgoing.get(u, []):
            ancestors[v].add(u)
            ancestors[v].update(ancestors[u])
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    return ancestors


def _compute_reachable(entry_ids: list[str], outgoing: dict[str, list[str]]) -> set[str]:
    visited: set[str] = set()
    queue = deque(entry_ids)
    while queue:
        u = queue.popleft()
        if u in visited:
            continue
        visited.add(u)
        for v in outgoing.get(u, []):
            if v not in visited:
                queue.append(v)
    return visited


def _contains_expression(value: Any) -> bool:
    if isinstance(value, str):
        return bool(TEMPLATE_PATTERN.search(value))
    if isinstance(value, list):
        return any(_contains_expression(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_expression(v) for v in value.values())
    return False


def _validate_static_config(
    node: NodeDefinition,
    node_cls: type[BaseNode[Any, Any]],
    *,
    issues: list[ValidationIssue],
    node_path: str,
) -> None:
    """Validate static config fields against the node's Pydantic config_model.

    Fields containing runtime {{ ... }} expressions are validated for presence only;
    pure literal fields are type- and constraint-checked immediately.
    """
    model_cls = node_cls.config_model
    model_fields = model_cls.model_fields

    # Check unknown keys if extra='forbid'
    if model_cls.model_config.get("extra") == "forbid":
        for key in node.config:
            if key not in model_fields:
                issues.append(
                    ValidationIssue(
                        code="INVALID_NODE_CONFIG",
                        severity=Severity.ERROR,
                        message=f"Unknown config field '{key}' on node '{node.id}'.",
                        node_id=node.id,
                        path=f"{node_path}.config.{key}",
                    )
                )

    # Check required fields are present (even if populated via an expression)
    for field_name, field_info in model_fields.items():
        if field_info.is_required() and field_name not in node.config:
            issues.append(
                ValidationIssue(
                    code="MISSING_REQUIRED_CONFIG",
                    severity=Severity.ERROR,
                    message=(
                        f"Required config field '{field_name}' is missing on node '{node.id}'."
                    ),
                    node_id=node.id,
                    path=f"{node_path}.config.{field_name}",
                )
            )

    # If no expressions appear in config at all, validate the full Pydantic model directly
    if not _contains_expression(node.config):
        try:
            model_cls.model_validate(node.config)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(part) for part in err.get("loc", ()))
                # Skip duplicate missing/extra errors already reported cleanly above
                if err.get("type") in {"missing", "extra_forbidden"}:
                    continue
                issues.append(
                    ValidationIssue(
                        code="INVALID_NODE_CONFIG",
                        severity=Severity.ERROR,
                        message=f"Invalid config at '{loc}': {err.get('msg')}",
                        node_id=node.id,
                        path=f"{node_path}.config.{loc}" if loc else f"{node_path}.config",
                    )
                )


def _scan_plaintext_secrets(
    value: Any,
    *,
    node_id: str,
    path: str,
    issues: list[ValidationIssue],
) -> None:
    if isinstance(value, str):
        if TEMPLATE_PATTERN.search(value):
            return
        for label, pattern in _PLAINTEXT_SECRET_PATTERNS:
            if pattern.search(value):
                issues.append(
                    ValidationIssue(
                        code="PLAINTEXT_SECRET_DETECTED",
                        severity=Severity.ERROR,
                        message=(
                            f"Potential plaintext secret ({label}) detected in node '{node_id}'. "
                            "Store secrets in encrypted credentials instead of workflow JSON."
                        ),
                        node_id=node_id,
                        path=path,
                    )
                )
                return
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _scan_plaintext_secrets(item, node_id=node_id, path=f"{path}[{idx}]", issues=issues)
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_plaintext_secrets(v, node_id=node_id, path=f"{path}.{k}", issues=issues)
