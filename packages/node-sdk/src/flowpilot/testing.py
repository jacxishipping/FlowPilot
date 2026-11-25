"""Testing utilities for isolated unit tests of FlowPilot nodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx

from flowpilot.context import ExecutionContext, LogEntry
from flowpilot.credentials import ResolvedCredential, SecretValue
from flowpilot.errors import CredentialError
from flowpilot.http import ControlledHttpClient
from flowpilot.node import BaseNode
from flowpilot.schemas import NodeResult
from flowpilot_schema import UsageRecord


class InMemoryCredentialResolver:
    """Test double for CredentialResolver enforcing tenant organization checks."""

    def __init__(self) -> None:
        self._store: dict[UUID, ResolvedCredential] = {}

    def add(
        self,
        *,
        organization_id: UUID,
        credential_type: str,
        secrets: dict[str, str],
        credential_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
        scopes: tuple[str, ...] = (),
    ) -> UUID:
        cid = credential_id or uuid4()
        self._store[cid] = ResolvedCredential(
            credential_id=cid,
            organization_id=organization_id,
            credential_type=credential_type,
            secrets={k: SecretValue(v) for k, v in secrets.items()},
            metadata=metadata or {},
            scopes=scopes,
        )
        return cid

    async def resolve(
        self,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> ResolvedCredential:
        cred = self._store.get(credential_id)
        if cred is None:
            raise CredentialError(f"Credential '{credential_id}' not found.")
        if cred.organization_id != organization_id:
            raise CredentialError(
                f"Credential '{credential_id}' is not accessible to org '{organization_id}'."
            )
        return cred


@dataclass
class NodeTestResult:
    result: NodeResult
    logs: list[LogEntry]
    usage_records: list[UsageRecord]


class NodeTestHarness:
    """Ergonomic test harness to execute a single BaseNode with mocked deps."""

    def __init__(
        self,
        *,
        organization_id: UUID | None = None,
        credential_resolver: InMemoryCredentialResolver | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        allowed_domains: tuple[str, ...] = (),
        allow_private_networks: bool = False,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.organization_id = organization_id or uuid4()
        self.credential_resolver = credential_resolver or InMemoryCredentialResolver()
        self.http_transport = http_transport
        self.allowed_domains = allowed_domains
        self.allow_private_networks = allow_private_networks
        self.services = services or {}

    async def run(
        self,
        node: BaseNode[Any, Any],
        config: dict[str, Any] | None = None,
        *,
        credential_bindings: dict[str, UUID] | None = None,
        trigger_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> NodeTestResult:
        validated_config = node.config_model.model_validate(config or {})
        http_client = ControlledHttpClient(
            allowed_domains=self.allowed_domains,
            allow_private_networks=self.allow_private_networks,
            transport=self.http_transport,
        )
        ctx = ExecutionContext(
            run_id=uuid4(),
            organization_id=self.organization_id,
            workflow_id=uuid4(),
            workflow_version_id=uuid4(),
            node_id="test_node",
            idempotency_key=idempotency_key,
            credential_bindings=credential_bindings,
            credential_resolver=self.credential_resolver,
            http_client=http_client,
            trigger_payload=trigger_payload,
            services=self.services,
        )
        raw = await node.execute(validated_config, ctx)
        if isinstance(raw, NodeResult):
            normalized = raw
        elif isinstance(raw, dict):
            normalized = NodeResult(output=raw)
        else:
            normalized = NodeResult(output=raw.model_dump(mode="json"))
        return NodeTestResult(
            result=normalized,
            logs=ctx.logs,
            usage_records=ctx.usage_records,
        )
