"""Temporal dispatch adapter wiring FastAPI run/approval endpoints to Temporal workflows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flowpilot_engine import ActivityTask, ApprovalSignal, task_queue_for_pool
from flowpilot_schema import PayloadRef, WorkerPool, WorkflowVersion


class TemporalDispatchClient:
    """Starts and signals FlowPilotDurableWorkflow executions via a Temporal client."""

    def __init__(
        self,
        *,
        address: str,
        namespace: str = "default",
        client: Any | None = None,
    ) -> None:
        self._address = address
        self._namespace = namespace
        self._client = client

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from temporalio.client import Client

        self._client = await Client.connect(self._address, namespace=self._namespace)
        return self._client

    async def start_workflow_run(
        self,
        *,
        run_id: UUID,
        version: WorkflowVersion,
        trigger_input: dict[str, Any],
    ) -> tuple[str, str]:
        """Start FlowPilotDurableWorkflow and return (temporal_workflow_id, temporal_run_id)."""
        client = await self._get_client()
        wf_id = f"flowpilot-run-{run_id}"
        tasks = [
            ActivityTask(
                run_id=run_id,
                organization_id=version.organization_id,
                workflow_id=version.workflow_id,
                workflow_version_id=version.id,
                node_id=node.id,
                node_type=node.type,
                worker_pool=node.policy.worker_pool or WorkerPool.WORKFLOW,
                resolved_config_ref=PayloadRef(
                    inline={**node.config, "_trigger_input": trigger_input}
                ),
                credential_bindings=node.credentials,
                timeout_ms=node.policy.timeout_ms,
            ).model_dump(mode="json")
            for node in version.definition.graph.nodes
        ]
        handle = await client.start_workflow(
            "FlowPilotDurableWorkflow",
            tasks,
            id=wf_id,
            task_queue=task_queue_for_pool(WorkerPool.WORKFLOW),
        )
        t_run_id = getattr(handle, "first_execution_run_id", None) or str(run_id)
        return wf_id, str(t_run_id)

    async def signal_approval(
        self,
        *,
        temporal_workflow_id: str,
        signal: ApprovalSignal,
    ) -> None:
        client = await self._get_client()
        handle = client.get_workflow_handle(temporal_workflow_id)
        await handle.signal("approval_decision", signal.model_dump(mode="json"))

    async def cancel_workflow(self, *, temporal_workflow_id: str) -> None:
        client = await self._get_client()
        handle = client.get_workflow_handle(temporal_workflow_id)
        await handle.signal("cancel_run")
