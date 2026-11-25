"""Temporal activities, workflow definition, and worker runner wired to FlowPilot engine."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy as TemporalRetryPolicy
from temporalio.worker import Worker

from flowpilot import (
    ApprovalRequiredError,
    ControlledHttpClient,
    ExecutionContext,
    FlowPilotNodeError,
    NodeRegistry,
    NodeResult,
)
from flowpilot_api.config import Settings
from flowpilot_api.db.session import create_engine_and_sessionmaker
from flowpilot_api.db.stores import SqlAlchemyCredentialResolver
from flowpilot_credentials import EnvelopeCipher, LocalMasterKeyProvider
from flowpilot_engine import (
    ActivityOutcome,
    ActivityTask,
    ApprovalSignal,
    PayloadManager,
    task_queue_for_pool,
)
from flowpilot_nodes_core import create_default_registry
from flowpilot_schema import ErrorDetail, WorkerPool

logger = logging.getLogger("flowpilot.worker")


class NodeActivityExecutor:
    """Stateless activity executor running typed FlowPilot nodes inside a Temporal worker pool."""

    def __init__(
        self,
        *,
        registry: NodeRegistry,
        credential_resolver: SqlAlchemyCredentialResolver,
        payload_manager: PayloadManager | None = None,
        allow_private_networks: bool = False,
    ) -> None:
        self._registry = registry
        self._credential_resolver = credential_resolver
        self._payloads = payload_manager or PayloadManager()
        self._allow_private_networks = allow_private_networks

    @activity.defn(name="flowpilot.execute_node")
    async def execute_node(self, task_json: dict[str, Any]) -> dict[str, Any]:
        """Temporal activity entrypoint executing a single FlowPilot node."""
        task = ActivityTask.model_validate(task_json)
        node_cls = self._registry.require(task.node_type)
        node = node_cls()

        resolved_config_dict = await self._payloads.unpack(task.resolved_config_ref) or {}
        typed_config = node_cls.config_model.model_validate(resolved_config_dict)

        ctx = ExecutionContext(
            run_id=task.run_id,
            organization_id=task.organization_id,
            workflow_id=task.workflow_id,
            workflow_version_id=task.workflow_version_id,
            node_id=task.node_id,
            attempt=task.attempt,
            idempotency_key=task.idempotency_key,
            credential_bindings=task.credential_bindings,
            credential_resolver=self._credential_resolver,
            http_client=ControlledHttpClient(allow_private_networks=self._allow_private_networks),
        )

        t0 = time.monotonic()
        try:
            raw = await node.execute(typed_config, ctx)
        except ApprovalRequiredError as sig:
            outcome = ActivityOutcome(
                succeeded=False,
                approval_required={
                    "title": sig.title,
                    "description": sig.description,
                    "risk_level": sig.risk_level.value,
                    "payload": sig.payload,
                },
                duration_ms=max(0, int((time.monotonic() - t0) * 1000)),
            )
            return outcome.model_dump(mode="json")
        except FlowPilotNodeError as err:
            outcome = ActivityOutcome(
                succeeded=False,
                error=ErrorDetail(
                    code=err.code,
                    message=ctx.redact(err.message),
                    retryable=err.retryable,
                    node_id=task.node_id,
                    details=ctx.redact(err.details),
                ),
                duration_ms=max(0, int((time.monotonic() - t0) * 1000)),
            )
            return outcome.model_dump(mode="json")

        duration_ms = max(0, int((time.monotonic() - t0) * 1000))
        if isinstance(raw, NodeResult):
            normalized = raw
        elif isinstance(raw, dict):
            normalized = NodeResult(output=raw)
        else:
            normalized = NodeResult(output=raw.model_dump(mode="json"))

        out_ref = await self._payloads.pack(
            organization_id=task.organization_id,
            run_id=task.run_id,
            payload=ctx.redact(normalized.output),
        )
        return ActivityOutcome(
            succeeded=True,
            output_ref=out_ref,
            branch_taken=normalized.branch,
            duration_ms=duration_ms,
        ).model_dump(mode="json")


@workflow.defn(name="FlowPilotDurableWorkflow")
class FlowPilotDurableWorkflow:
    """Durable Temporal workflow handling approval signals and dispatching node activities."""

    def __init__(self) -> None:
        self._approvals: dict[str, dict[str, Any]] = {}
        self._canceled = False

    @workflow.signal(name="approval_decision")
    async def on_approval_decision(self, signal_json: dict[str, Any]) -> None:
        signal = ApprovalSignal.model_validate(signal_json)
        self._approvals[str(signal.approval_id)] = signal.model_dump(mode="json")

    @workflow.signal(name="cancel_run")
    async def on_cancel_run(self) -> None:
        self._canceled = True

    @workflow.run
    async def run(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for task_json in tasks:
            if self._canceled:
                break
            task = ActivityTask.model_validate(task_json)
            outcome = await workflow.execute_activity(
                "flowpilot.execute_node",
                task_json,
                task_queue=task_queue_for_pool(task.worker_pool),
                start_to_close_timeout=timedelta(milliseconds=task.timeout_ms),
                retry_policy=TemporalRetryPolicy(maximum_attempts=1),
            )
            results.append(outcome)
        return results


async def run_worker(pool: WorkerPool | None = None) -> None:
    """Connect to Temporal and start polling the configured FlowPilot worker pool queue."""
    settings = Settings()
    selected_pool = pool or WorkerPool(os.environ.get("FLOWPILOT_WORKER_POOL", "workflow"))
    queue = task_queue_for_pool(selected_pool)

    _, session_factory = create_engine_and_sessionmaker(settings.database_url)
    cipher = EnvelopeCipher(
        LocalMasterKeyProvider(
            active_key_id=settings.active_key_id,
            master_secret=settings.master_key,
        )
    )
    executor = NodeActivityExecutor(
        registry=create_default_registry(),
        credential_resolver=SqlAlchemyCredentialResolver(session_factory, cipher),
        allow_private_networks=settings.allow_private_networks,
    )

    logger.info(
        "Connecting Temporal worker to %s (namespace=%s, task_queue=%s)",
        settings.temporal_address,
        settings.temporal_namespace,
        queue,
    )
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )

    workflows_list = [FlowPilotDurableWorkflow] if selected_pool == WorkerPool.WORKFLOW else []
    worker = Worker(
        client,
        task_queue=queue,
        workflows=workflows_list,
        activities=[executor.execute_node],
    )
    logger.info("FlowPilot Temporal worker listening on queue '%s'", queue)
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


__all__ = [
    "UUID",
    "FlowPilotDurableWorkflow",
    "NodeActivityExecutor",
    "main",
    "run_worker",
]
