"""First-party parameterized SQL query node with read-only default and row caps."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from pydantic import Field

from flowpilot import (
    BaseNode,
    ConfigurationError,
    ExecutionContext,
    NodeConfigModel,
    NodeMetadata,
    NodePackageManifest,
    NodeRegistry,
    NodeResult,
    SecurityPolicyError,
)
from flowpilot_schema import NodeKind, RiskLevel, SideEffectPolicy, WorkerPool

DbExecutorFn = Callable[[str, dict[str, Any], int], Awaitable[list[dict[str, Any]]]]

_MUTATION_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


class DatabaseQueryConfig(NodeConfigModel):
    query: str = Field(..., min_length=4, max_length=8000)
    params: dict[str, Any] = Field(default_factory=dict)
    max_rows: int = Field(default=100, ge=1, le=5000)
    allow_mutations: bool = False


class DatabaseQueryNode(BaseNode[DatabaseQueryConfig, Any]):
    metadata: ClassVar[NodeMetadata] = NodeMetadata(
        type="database.query",
        name="Database Query",
        description="Executes a parameterized SQL query with read-only enforcement and row caps.",
        category="database",
        kind=NodeKind.ACTION,
        worker_pool=WorkerPool.INTEGRATIONS,
        side_effect=SideEffectPolicy.NONE,
        risk_level=RiskLevel.MEDIUM,
        required_permissions=("database.query",),
    )
    config_model: ClassVar[type[DatabaseQueryConfig]] = DatabaseQueryConfig

    async def execute(self, config: DatabaseQueryConfig, ctx: ExecutionContext) -> NodeResult:
        cleaned = config.query.strip()
        # Reject multi-statement SQL injection attempts
        if ";" in cleaned.rstrip(";"):
            raise SecurityPolicyError(
                "Multiple SQL statements are forbidden in a single DatabaseQueryNode.",
                code="SQL_MULTI_STATEMENT_BLOCKED",
            )
        if not config.allow_mutations and _MUTATION_PATTERN.search(cleaned):
            raise SecurityPolicyError(
                "Mutating SQL statement blocked while allow_mutations=False.",
                code="SQL_MUTATION_BLOCKED",
            )

        executor: DbExecutorFn | None = ctx.services.get("db_executor")
        if executor is None:
            raise ConfigurationError(
                "No 'db_executor' service configured in ExecutionContext.services."
            )

        rows = await executor(cleaned, dict(config.params), config.max_rows)
        capped = rows[: config.max_rows]
        ctx.log("INFO", f"Database query returned {len(capped)} row(s)")
        return NodeResult(
            output={
                "rows": capped,
                "row_count": len(capped),
                "truncated": len(rows) > config.max_rows,
            }
        )


DATABASE_PACKAGE_MANIFEST = NodePackageManifest(
    package_name="flowpilot-nodes-database",
    package_version="0.1.0",
    sdk_compatibility=">=0.1.0,<1.0.0",
    first_party=True,
    entrypoints={"database.query": "flowpilot_nodes_database:DatabaseQueryNode"},
    permissions=("database.query",),
    credential_types=(),
)


def register_database_nodes(registry: NodeRegistry, *, replace: bool = False) -> NodeRegistry:
    registry.register(DatabaseQueryNode, replace=replace)
    return registry


__all__ = [
    "DATABASE_PACKAGE_MANIFEST",
    "DatabaseQueryConfig",
    "DatabaseQueryNode",
    "DbExecutorFn",
    "register_database_nodes",
]
