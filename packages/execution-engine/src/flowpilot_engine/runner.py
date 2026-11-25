"""Durable workflow execution coordinator built on GraphInterpreter and Node SDK."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from flowpilot import (
    ApprovalRequiredError,
    ConfigurationError,
    ControlledHttpClient,
    CredentialResolver,
    ExecutionContext,
    FlowPilotNodeError,
    LogEntry,
    NodeRegistry,
    NodeResult,
    NodeTimeoutError,
)
from flowpilot_engine.events import EventSink, InMemoryEventSink
from flowpilot_engine.interpreter import GraphInterpreter
from flowpilot_engine.retry import evaluate_retry
from flowpilot_engine.state import transition_node_status, transition_run_status
from flowpilot_engine.store import ExecutionStore, InMemoryExecutionStore, PayloadManager
from flowpilot_expressions import (
    ExpressionError,
    evaluate_expression,
    resolve_template,
    resolve_value,
)
from flowpilot_schema import (
    ApprovalDecision,
    ApprovalRequest,
    EdgeKind,
    ErrorDetail,
    EventType,
    ExecutionEvent,
    NodeDefinition,
    NodeExecution,
    NodeStatus,
    RunStatus,
    UsageRecord,
    WorkflowGraph,
    WorkflowRun,
    WorkflowVersion,
)
from flowpilot_schema.execution import utc_now
from flowpilot_validator import WorkflowValidator

SleepFn = Callable[[float], Awaitable[None]]


class WorkflowExecutionError(RuntimeError):
    """Raised on coordinator-level access, validation, or orchestration errors."""


class WorkflowExecutionEngine:
    """Executes immutable WorkflowVersions deterministically with full lifecycle guarantees."""

    def __init__(
        self,
        *,
        registry: NodeRegistry,
        store: ExecutionStore | None = None,
        event_sink: EventSink | None = None,
        payload_manager: PayloadManager | None = None,
        credential_resolver: CredentialResolver | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        allow_private_networks: bool = False,
        env: dict[str, Any] | None = None,
        services: dict[str, Any] | None = None,
        sleep_fn: SleepFn | None = None,
        rng_seed: int | None = None,
        validate_before_run: bool = True,
    ) -> None:
        self._registry = registry
        self._store = store or InMemoryExecutionStore()
        self._event_sink = event_sink or InMemoryEventSink()
        self._payloads = payload_manager or PayloadManager()
        self._credential_resolver = credential_resolver
        self._http_transport = http_transport
        self._allow_private_networks = allow_private_networks
        self._env = dict(env or {})
        self._services = dict(services or {})
        self._sleep: SleepFn = sleep_fn or asyncio.sleep
        self._rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
        self._validate_before_run = validate_before_run
        self._validator = WorkflowValidator(registry)
        self._event_seq: dict[UUID, int] = {}
        self._canceled_runs: set[UUID] = set()

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def event_sink(self) -> EventSink:
        return self._event_sink

    @property
    def payloads(self) -> PayloadManager:
        return self._payloads

    async def start_run(
        self,
        version: WorkflowVersion,
        *,
        organization_id: UUID | None = None,
        trigger_input: dict[str, Any] | None = None,
        trigger_type: str = "manual",
        idempotency_key: str | None = None,
    ) -> WorkflowRun:
        """Start a new run pinned to an immutable WorkflowVersion."""
        org_id = organization_id or version.organization_id
        if org_id != version.organization_id:
            raise WorkflowExecutionError(
                f"Tenant isolation violation: org '{org_id}' cannot execute workflow version "
                f"owned by org '{version.organization_id}'."
            )

        if self._validate_before_run:
            validation = self._validator.validate(version.definition)
            if not validation.is_valid:
                codes = ", ".join(sorted(validation.error_codes()))
                raise WorkflowExecutionError(
                    f"Cannot execute invalid workflow version '{version.id}': {codes}"
                )

        # Idempotent run creation deduplication
        if idempotency_key:
            existing = await self._store.find_run_by_idempotency_key(
                organization_id=org_id,
                workflow_id=version.workflow_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return existing

        await self._store.save_version(version)

        run_id_holder = WorkflowRun(
            organization_id=org_id,
            workflow_id=version.workflow_id,
            workflow_version_id=version.id,
            status=RunStatus.QUEUED,
            trigger_type=trigger_type,
            idempotency_key=idempotency_key,
        )
        run_id_holder.input_ref = await self._payloads.pack(
            organization_id=org_id,
            run_id=run_id_holder.id,
            payload=trigger_input or {},
            inline_limit_bytes=version.definition.settings.payload_inline_limit_bytes,
        )
        for node in version.definition.graph.nodes:
            node_cls = self._registry.get(node.type)
            pool = node.policy.worker_pool or (
                node_cls.metadata.worker_pool if node_cls else None
            )
            default_pool = NodeExecution.model_fields["worker_pool"].default
            run_id_holder.node_executions[node.id] = NodeExecution(
                run_id=run_id_holder.id,
                node_id=node.id,
                node_type=node.type,
                status=NodeStatus.PENDING,
                worker_pool=pool if pool is not None else default_pool,
            )

        run_id_holder.usage_records.append(
            UsageRecord(
                organization_id=org_id,
                run_id=run_id_holder.id,
                metric="workflow_run",
                quantity=1.0,
                unit="run",
                metadata={"workflow_version_id": str(version.id)},
            )
        )

        await self._store.save_run(run_id_holder)
        await self._emit(
            run_id_holder,
            EventType.RUN_CREATED,
            payload={"workflow_version_id": str(version.id), "trigger_type": trigger_type},
        )

        return await self._drive_run(run_id_holder, version)

    async def submit_approval_decision(
        self,
        run_id: UUID,
        approval_id: UUID,
        *,
        organization_id: UUID,
        approved: bool,
        decided_by: str,
        comment: str = "",
    ) -> WorkflowRun:
        """Deliver a human approval decision signal and resume a paused run."""
        run = await self._store.get_run(run_id)
        if run is None:
            raise WorkflowExecutionError(f"Workflow run '{run_id}' not found.")
        if run.organization_id != organization_id:
            raise WorkflowExecutionError(
                f"Tenant isolation violation: org '{organization_id}' cannot decide approval "
                f"for run '{run_id}'."
            )
        if run.status != RunStatus.WAITING_APPROVAL:
            raise WorkflowExecutionError(
                f"Run '{run_id}' is in state {run.status.value}, not WAITING_APPROVAL."
            )

        approval = run.approvals.get(approval_id)
        if approval is None:
            raise WorkflowExecutionError(
                f"Approval '{approval_id}' not found on run '{run_id}'."
            )
        if approval.decision != ApprovalDecision.PENDING:
            raise WorkflowExecutionError(
                f"Approval '{approval_id}' was already decided ({approval.decision.value})."
            )

        approval.decision = ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED
        approval.decided_by = decided_by
        approval.decided_at = utc_now()
        approval.comment = comment

        # Re-queue the paused node to READY so it resumes with the decision attached
        node_exec = run.node_executions[approval.node_id]
        node_exec.status = transition_node_status(node_exec.status, NodeStatus.READY)

        run.status = transition_run_status(run.status, RunStatus.RUNNING)
        await self._store.save_run(run)
        await self._emit(
            run,
            EventType.APPROVAL_DECIDED,
            node_id=approval.node_id,
            payload={
                "approval_id": str(approval_id),
                "decision": approval.decision.value,
                "decided_by": decided_by,
            },
        )
        await self._emit(run, EventType.RUN_RESUMED, node_id=approval.node_id)

        version = await self._store.get_version(run.workflow_version_id)
        if version is None:
            raise WorkflowExecutionError(
                f"Pinned workflow version '{run.workflow_version_id}' not found."
            )
        return await self._drive_run(run, version)

    async def cancel_run(
        self,
        run_id: UUID,
        *,
        organization_id: UUID,
        reason: str = "Canceled by user",
    ) -> WorkflowRun:
        """Cancel a queued, running, or paused workflow run."""
        run = await self._store.get_run(run_id)
        if run is None:
            raise WorkflowExecutionError(f"Workflow run '{run_id}' not found.")
        if run.organization_id != organization_id:
            raise WorkflowExecutionError(
                f"Tenant isolation violation: org '{organization_id}' cannot cancel run '{run_id}'."
            )
        if run.status.is_terminal:
            return run

        self._canceled_runs.add(run_id)
        run.status = transition_run_status(run.status, RunStatus.CANCELED)
        run.finished_at = utc_now()
        run.error = ErrorDetail(code="RUN_CANCELED", message=reason, retryable=False)

        for node_exec in run.node_executions.values():
            if not node_exec.status.is_terminal:
                node_exec.status = transition_node_status(node_exec.status, NodeStatus.CANCELED)
                node_exec.finished_at = utc_now()
                await self._emit(run, EventType.NODE_CANCELED, node_id=node_exec.node_id)

        await self._store.save_run(run)
        await self._emit(run, EventType.RUN_CANCELED, payload={"reason": reason})
        return run

    # --- Core Orchestration Loop ---

    async def _drive_run(self, run: WorkflowRun, version: WorkflowVersion) -> WorkflowRun:
        definition = version.definition
        interpreter = GraphInterpreter(definition.graph)
        trigger_payload = await self._payloads.unpack(run.input_ref) or {}
        inline_limit = definition.settings.payload_inline_limit_bytes

        if run.status == RunStatus.QUEUED:
            run.status = transition_run_status(run.status, RunStatus.RUNNING)
            run.started_at = utc_now()
            await self._store.save_run(run)
            await self._emit(run, EventType.RUN_STARTED)

        # Preload already-completed node outputs (critical when resuming from WAITING_APPROVAL!)
        node_outputs: dict[str, dict[str, Any]] = {}
        for nid, nexec in run.node_executions.items():
            if nexec.status == NodeStatus.SUCCEEDED:
                out = await self._payloads.unpack(nexec.output_ref)
                node_outputs[nid] = {"output": out}
            elif nexec.status == NodeStatus.FAILED and nexec.error is not None:
                node_outputs[nid] = {"output": None, "error": nexec.error.model_dump(mode="json")}

        deadline = time.monotonic() + (definition.settings.timeout_ms / 1000.0)
        semaphore = asyncio.Semaphore(definition.settings.max_concurrent_nodes)

        while True:
            if run.id in self._canceled_runs:
                return await self.cancel_run(
                    run.id, organization_id=run.organization_id, reason="Run canceled"
                )

            if time.monotonic() > deadline:
                run.status = transition_run_status(run.status, RunStatus.TIMED_OUT)
                run.finished_at = utc_now()
                run.error = ErrorDetail(
                    code="WORKFLOW_TIMEOUT",
                    message=(
                        f"Workflow run exceeded timeout of {definition.settings.timeout_ms}ms."
                    ),
                    retryable=False,
                )
                await self._store.save_run(run)
                await self._emit(
                    run,
                    EventType.RUN_TIMED_OUT,
                    payload={"timeout_ms": definition.settings.timeout_ms},
                )
                return run

            step = interpreter.compute_step(run.node_executions)

            # Mark newly skipped nodes
            for skipped_id in step.skipped_node_ids:
                nexec = run.node_executions[skipped_id]
                if nexec.status != NodeStatus.SKIPPED:
                    nexec.status = transition_node_status(nexec.status, NodeStatus.SKIPPED)
                    nexec.finished_at = utc_now()
                    await self._emit(run, EventType.NODE_SKIPPED, node_id=skipped_id)

            # Fail immediately on unhandled node failure
            if step.unhandled_failed_node_ids:
                failed_id = step.unhandled_failed_node_ids[0]
                failed_exec = run.node_executions[failed_id]
                run.status = transition_run_status(run.status, RunStatus.FAILED)
                run.finished_at = utc_now()
                run.error = failed_exec.error or ErrorDetail(
                    code="NODE_FAILED",
                    message=f"Node '{failed_id}' failed.",
                    node_id=failed_id,
                )
                await self._store.save_run(run)
                await self._emit(
                    run,
                    EventType.RUN_FAILED,
                    node_id=failed_id,
                    payload={"error": run.error.model_dump(mode="json")},
                )
                return run

            if step.is_complete:
                final_output = self._compute_run_output(
                    definition_output_mapping=definition.output_mapping,
                    node_outputs=node_outputs,
                    trigger_payload=trigger_payload,
                    run=run,
                )
                run.output_ref = await self._payloads.pack(
                    organization_id=run.organization_id,
                    run_id=run.id,
                    payload=final_output,
                    inline_limit_bytes=inline_limit,
                )
                run.status = transition_run_status(run.status, RunStatus.SUCCEEDED)
                run.finished_at = utc_now()
                await self._store.save_run(run)
                await self._emit(run, EventType.RUN_SUCCEEDED)
                return run

            if not step.ready_node_ids:
                # Deadlock guard (should be prevented by validator)
                run.status = transition_run_status(run.status, RunStatus.FAILED)
                run.finished_at = utc_now()
                run.error = ErrorDetail(
                    code="SCHEDULER_STALLED",
                    message="No ready nodes available while graph is incomplete.",
                )
                await self._store.save_run(run)
                await self._emit(run, EventType.RUN_FAILED)
                return run

            # Transition newly ready nodes
            for nid in step.ready_node_ids:
                nexec = run.node_executions[nid]
                if nexec.status == NodeStatus.PENDING:
                    nexec.status = transition_node_status(nexec.status, NodeStatus.READY)
                    await self._emit(run, EventType.NODE_READY, node_id=nid)

            await self._store.save_run(run)

            # Execute ready batch concurrently up to max_concurrent_nodes
            async def _run_guarded(target_id: str) -> ApprovalRequiredError | None:
                async with semaphore:
                    return await self._execute_node_in_graph(
                        run=run,
                        node=interpreter.nodes_by_id[target_id],
                        interpreter=interpreter,
                        node_outputs=node_outputs,
                        trigger_payload=trigger_payload,
                        inline_limit=inline_limit,
                        loop_vars=None,
                    )

            approval_signals = await asyncio.gather(
                *(_run_guarded(nid) for nid in step.ready_node_ids)
            )

            # If any node requested human approval, pause run cleanly in WAITING_APPROVAL
            paused_requests = [
                (nid, sig)
                for nid, sig in zip(step.ready_node_ids, approval_signals, strict=True)
                if sig is not None
            ]
            if paused_requests:
                run.status = transition_run_status(run.status, RunStatus.WAITING_APPROVAL)
                for nid, sig in paused_requests:
                    approval_req = ApprovalRequest(
                        organization_id=run.organization_id,
                        run_id=run.id,
                        node_id=nid,
                        title=sig.title,
                        description=sig.description,
                        risk_level=sig.risk_level,
                        payload=sig.payload,
                    )
                    run.approvals[approval_req.id] = approval_req
                    await self._emit(
                        run,
                        EventType.APPROVAL_REQUESTED,
                        node_id=nid,
                        payload={
                            "approval_id": str(approval_req.id),
                            "title": approval_req.title,
                            "risk_level": approval_req.risk_level.value,
                        },
                    )
                await self._store.save_run(run)
                await self._emit(run, EventType.RUN_WAITING_APPROVAL)
                return run

            await self._store.save_run(run)

    async def _execute_node_in_graph(
        self,
        *,
        run: WorkflowRun,
        node: NodeDefinition,
        interpreter: GraphInterpreter,
        node_outputs: dict[str, dict[str, Any]],
        trigger_payload: dict[str, Any],
        inline_limit: int,
        loop_vars: dict[str, Any] | None,
    ) -> ApprovalRequiredError | None:
        nexec = run.node_executions[node.id]
        node_cls = self._registry.require(node.type)
        node_instance = node_cls()

        # Collect upstream error context if invoked via an ERROR edge
        error_ctx = self._build_error_context(node.id, interpreter, run.node_executions)
        expr_context: dict[str, Any] = {
            "nodes": node_outputs,
            "trigger": trigger_payload,
            "inputs": trigger_payload,
            "env": self._env,
            "loop": loop_vars or {},
            "run": {
                "id": str(run.id),
                "organization_id": str(run.organization_id),
                "workflow_id": str(run.workflow_id),
                "workflow_version_id": str(run.workflow_version_id),
            },
            "error": error_ctx,
        }

        # Check if this node already has a decided approval (resuming after pause)
        prior_approval = self._find_latest_approval_for_node(run, node.id)

        # Policy-level approval gate on ANY node (policy.require_approval=True)
        if node.policy.require_approval and node_cls.metadata.kind.value != "approval":
            if prior_approval is None:
                nexec.status = transition_node_status(nexec.status, NodeStatus.RUNNING)
                return ApprovalRequiredError(
                    title=f"Approval required for node '{node.name or node.id}'",
                    description=(
                        f"Node '{node.id}' ({node.type}) requires explicit human approval "
                        "before execution."
                    ),
                    risk_level=node_cls.metadata.risk_level,
                    payload={"node_id": node.id, "node_type": node.type},
                )
            if prior_approval.decision == ApprovalDecision.REJECTED:
                nexec.status = transition_node_status(nexec.status, NodeStatus.RUNNING)
                nexec.status = transition_node_status(nexec.status, NodeStatus.FAILED)
                nexec.finished_at = utc_now()
                nexec.error = ErrorDetail(
                    code="APPROVAL_REJECTED",
                    message=prior_approval.comment or f"Approval rejected for node '{node.id}'.",
                    retryable=False,
                    node_id=node.id,
                )
                node_outputs[node.id] = {
                    "output": None,
                    "error": nexec.error.model_dump(mode="json"),
                }
                await self._emit(
                    run,
                    EventType.NODE_FAILED,
                    node_id=node.id,
                    payload={"error": nexec.error.model_dump(mode="json")},
                )
                return None

        # Resolve idempotency key expression if configured
        idempotency_key: str | None = None
        if node.policy.idempotency_key_expression:
            try:
                resolved_idem = resolve_template(
                    node.policy.idempotency_key_expression, expr_context
                )
                idempotency_key = None if resolved_idem is None else str(resolved_idem)
            except ExpressionError as exc:
                await self._fail_node(
                    run,
                    nexec,
                    node_outputs,
                    ErrorDetail(
                        code="IDEMPOTENCY_KEY_EXPRESSION_ERROR",
                        message=str(exc),
                        retryable=False,
                        node_id=node.id,
                    ),
                )
                return None
        nexec.idempotency_key = idempotency_key

        # Resolve config expressions
        try:
            resolved_config_dict = resolve_value(node.config, expr_context)
        except ExpressionError as exc:
            await self._fail_node(
                run,
                nexec,
                node_outputs,
                ErrorDetail(
                    code="EXPRESSION_EVALUATION_ERROR",
                    message=str(exc),
                    retryable=False,
                    node_id=node.id,
                ),
            )
            return None

        nexec.input_ref = await self._payloads.pack(
            organization_id=run.organization_id,
            run_id=run.id,
            payload=resolved_config_dict,
            inline_limit_bytes=inline_limit,
        )

        # Validate resolved config against Node SDK Pydantic model
        try:
            typed_config = node_cls.config_model.model_validate(resolved_config_dict)
        except ValidationError as exc:
            await self._fail_node(
                run,
                nexec,
                node_outputs,
                ErrorDetail(
                    code="CONFIGURATION_ERROR",
                    message=f"Invalid runtime config for node '{node.id}': {exc}",
                    retryable=False,
                    node_id=node.id,
                ),
            )
            return None

        # Native bounded loop execution when node_cls.metadata.is_loop is True
        if node_cls.metadata.is_loop:
            return await self._execute_loop_node(
                run=run,
                node=node,
                nexec=nexec,
                expr_context=expr_context,
                node_outputs=node_outputs,
                trigger_payload=trigger_payload,
                inline_limit=inline_limit,
            )

        # Standard node execution with timeout & side-effect-safe retry loop
        retry_policy = node.policy.retry
        timeout_seconds = node.policy.timeout_ms / 1000.0
        nexec.started_at = nexec.started_at or utc_now()

        while True:
            nexec.attempt += 1
            nexec.status = transition_node_status(nexec.status, NodeStatus.RUNNING)
            await self._emit(
                run,
                EventType.NODE_STARTED,
                node_id=node.id,
                payload={"attempt": nexec.attempt, "node_type": node.type},
            )

            http_client = ControlledHttpClient(
                allowed_domains=node.policy.allowed_domains,
                allow_private_networks=self._allow_private_networks,
                transport=self._http_transport,
            )

            def _on_log(entry: LogEntry) -> None:
                # Logs captured on ctx; emitted after execution or via sync buffer
                pass

            ctx = ExecutionContext(
                run_id=run.id,
                organization_id=run.organization_id,
                workflow_id=run.workflow_id,
                workflow_version_id=run.workflow_version_id,
                node_id=node.id,
                attempt=nexec.attempt,
                idempotency_key=idempotency_key,
                credential_bindings=node.credentials,
                credential_resolver=self._credential_resolver,
                http_client=http_client,
                trigger_payload=trigger_payload,
                approval_resolution=prior_approval,
                on_log=_on_log,
                is_canceled=lambda: run.id in self._canceled_runs,
                services=self._services,
            )

            t0 = time.monotonic()
            try:
                raw_result = await asyncio.wait_for(
                    node_instance.execute(typed_config, ctx),
                    timeout=timeout_seconds,
                )
            except ApprovalRequiredError as approval_sig:
                # Emit buffered logs before pausing
                await self._flush_ctx_telemetry(run, node.id, ctx, duration_ms=0)
                return approval_sig
            except TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                await self._flush_ctx_telemetry(run, node.id, ctx, duration_ms=elapsed_ms)
                err = NodeTimeoutError(
                    f"Node '{node.id}' timed out after {node.policy.timeout_ms}ms."
                )
                handled = await self._handle_node_error(
                    run=run,
                    node=node,
                    nexec=nexec,
                    node_outputs=node_outputs,
                    error=err,
                    side_effect=node_cls.metadata.side_effect,
                    retry_policy=retry_policy,
                    idempotency_key=idempotency_key,
                    ctx=ctx,
                )
                if handled:
                    continue
                return None
            except FlowPilotNodeError as err:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                await self._flush_ctx_telemetry(run, node.id, ctx, duration_ms=elapsed_ms)
                handled = await self._handle_node_error(
                    run=run,
                    node=node,
                    nexec=nexec,
                    node_outputs=node_outputs,
                    error=err,
                    side_effect=node_cls.metadata.side_effect,
                    retry_policy=retry_policy,
                    idempotency_key=idempotency_key,
                    ctx=ctx,
                )
                if handled:
                    continue
                return None
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                await self._flush_ctx_telemetry(run, node.id, ctx, duration_ms=elapsed_ms)
                wrapped = ConfigurationError(
                    ctx.redact(f"Unhandled exception in node '{node.id}': {exc}")
                )
                await self._handle_node_error(
                    run=run,
                    node=node,
                    nexec=nexec,
                    node_outputs=node_outputs,
                    error=wrapped,
                    side_effect=node_cls.metadata.side_effect,
                    retry_policy=retry_policy,
                    idempotency_key=idempotency_key,
                    ctx=ctx,
                )
                return None

            elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
            await self._flush_ctx_telemetry(run, node.id, ctx, duration_ms=elapsed_ms)

            # Normalize output and redact any known secrets defensively
            if isinstance(raw_result, NodeResult):
                normalized = raw_result
            elif isinstance(raw_result, dict):
                normalized = NodeResult(output=raw_result)
            else:
                normalized = NodeResult(output=raw_result.model_dump(mode="json"))

            safe_output = ctx.redact(normalized.output)
            nexec.output_ref = await self._payloads.pack(
                organization_id=run.organization_id,
                run_id=run.id,
                payload=safe_output,
                inline_limit_bytes=inline_limit,
            )
            nexec.branch_taken = normalized.branch
            nexec.status = transition_node_status(nexec.status, NodeStatus.SUCCEEDED)
            nexec.finished_at = utc_now()
            nexec.duration_ms = elapsed_ms
            node_outputs[node.id] = {"output": safe_output}

            await self._emit(
                run,
                EventType.NODE_SUCCEEDED,
                node_id=node.id,
                payload={"duration_ms": elapsed_ms, "branch": normalized.branch},
            )
            return None

    async def _execute_loop_node(
        self,
        *,
        run: WorkflowRun,
        node: NodeDefinition,
        nexec: NodeExecution,
        expr_context: dict[str, Any],
        node_outputs: dict[str, dict[str, Any]],
        trigger_payload: dict[str, Any],
        inline_limit: int,
    ) -> ApprovalRequiredError | None:
        assert node.loop is not None
        loop_spec = node.loop
        nexec.attempt += 1
        nexec.started_at = utc_now()
        nexec.status = transition_node_status(nexec.status, NodeStatus.RUNNING)
        await self._emit(run, EventType.NODE_STARTED, node_id=node.id, payload={"attempt": 1})

        t0 = time.monotonic()
        try:
            items = evaluate_expression(loop_spec.items_expression, expr_context)
        except ExpressionError as exc:
            await self._fail_node(
                run,
                nexec,
                node_outputs,
                ErrorDetail(
                    code="LOOP_ITEMS_EXPRESSION_ERROR",
                    message=str(exc),
                    retryable=False,
                    node_id=node.id,
                ),
            )
            return None

        if not isinstance(items, list):
            await self._fail_node(
                run,
                nexec,
                node_outputs,
                ErrorDetail(
                    code="LOOP_ITEMS_NOT_LIST",
                    message=(
                        "Loop items_expression must evaluate to a list, "
                        f"got {type(items).__name__}."
                    ),
                    retryable=False,
                    node_id=node.id,
                ),
            )
            return None

        if len(items) > loop_spec.max_iterations:
            await self._fail_node(
                run,
                nexec,
                node_outputs,
                ErrorDetail(
                    code="LOOP_MAX_ITERATIONS_EXCEEDED",
                    message=(
                        f"Loop '{node.id}' received {len(items)} items, exceeding "
                        f"max_iterations ({loop_spec.max_iterations})."
                    ),
                    retryable=False,
                    node_id=node.id,
                ),
            )
            return None

        iteration_results: list[dict[str, Any]] = []
        iteration_errors: list[dict[str, Any]] = []

        for idx, item in enumerate(items):
            if run.id in self._canceled_runs:
                nexec.status = transition_node_status(nexec.status, NodeStatus.CANCELED)
                nexec.finished_at = utc_now()
                return None

            loop_vars = {
                loop_spec.item_variable: item,
                loop_spec.index_variable: idx,
                "count": len(items),
            }
            sub_result, sub_error = await self._run_subgraph_iteration(
                parent_run=run,
                body_graph=loop_spec.body_graph,
                outer_outputs=node_outputs,
                trigger_payload=trigger_payload,
                loop_vars=loop_vars,
                inline_limit=inline_limit,
            )
            if sub_error is not None:
                if loop_spec.fail_fast:
                    await self._fail_node(
                        run,
                        nexec,
                        node_outputs,
                        ErrorDetail(
                            code="LOOP_ITERATION_FAILED",
                            message=(
                                f"Loop '{node.id}' failed at iteration {idx}: "
                                f"{sub_error.message}"
                            ),
                            retryable=False,
                            node_id=node.id,
                            details={"iteration": idx, "cause": sub_error.model_dump(mode="json")},
                        ),
                    )
                    return None
                iteration_errors.append(
                    {"index": idx, "error": sub_error.model_dump(mode="json")}
                )
            else:
                iteration_results.append(sub_result)

        elapsed_ms = max(0, int((time.monotonic() - t0) * 1000))
        output = {
            "results": iteration_results,
            "errors": iteration_errors,
            "count": len(items),
            "succeeded_count": len(iteration_results),
            "failed_count": len(iteration_errors),
        }
        nexec.output_ref = await self._payloads.pack(
            organization_id=run.organization_id,
            run_id=run.id,
            payload=output,
            inline_limit_bytes=inline_limit,
        )
        nexec.status = transition_node_status(nexec.status, NodeStatus.SUCCEEDED)
        nexec.finished_at = utc_now()
        nexec.duration_ms = elapsed_ms
        node_outputs[node.id] = {"output": output}
        run.usage_records.append(
            UsageRecord(
                organization_id=run.organization_id,
                run_id=run.id,
                node_id=node.id,
                metric="node_execution",
                quantity=1.0,
                unit="execution",
                metadata={"loop_iterations": len(items)},
            )
        )
        await self._emit(
            run,
            EventType.NODE_SUCCEEDED,
            node_id=node.id,
            payload={"duration_ms": elapsed_ms, "iterations": len(items)},
        )
        return None

    async def _run_subgraph_iteration(
        self,
        *,
        parent_run: WorkflowRun,
        body_graph: WorkflowGraph,
        outer_outputs: dict[str, dict[str, Any]],
        trigger_payload: dict[str, Any],
        loop_vars: dict[str, Any],
        inline_limit: int,
    ) -> tuple[dict[str, Any], ErrorDetail | None]:
        """Execute a single iteration of a loop's body_graph deterministically."""
        sub_interpreter = GraphInterpreter(body_graph)
        sub_run = WorkflowRun(
            id=parent_run.id,
            organization_id=parent_run.organization_id,
            workflow_id=parent_run.workflow_id,
            workflow_version_id=parent_run.workflow_version_id,
            status=RunStatus.RUNNING,
            node_executions={
                n.id: NodeExecution(
                    run_id=parent_run.id,
                    node_id=n.id,
                    node_type=n.type,
                    status=NodeStatus.PENDING,
                )
                for n in body_graph.nodes
            },
        )
        scoped_outputs: dict[str, dict[str, Any]] = dict(outer_outputs)

        while True:
            step = sub_interpreter.compute_step(sub_run.node_executions)
            for skipped_id in step.skipped_node_ids:
                sub_run.node_executions[skipped_id].status = NodeStatus.SKIPPED
            if step.unhandled_failed_node_ids:
                failed = sub_run.node_executions[step.unhandled_failed_node_ids[0]]
                return {}, failed.error or ErrorDetail(
                    code="SUBGRAPH_NODE_FAILED",
                    message=f"Node '{failed.node_id}' failed inside loop body.",
                )
            if step.is_complete:
                parent_run.usage_records.extend(sub_run.usage_records)
                # Return map of body node outputs (plus leaf output convenience)
                body_outputs = {
                    n.id: scoped_outputs.get(n.id, {}).get("output") for n in body_graph.nodes
                }
                return body_outputs, None

            for nid in step.ready_node_ids:
                nexec = sub_run.node_executions[nid]
                if nexec.status == NodeStatus.PENDING:
                    nexec.status = NodeStatus.READY

            for nid in step.ready_node_ids:
                await self._execute_node_in_graph(
                    run=sub_run,
                    node=sub_interpreter.nodes_by_id[nid],
                    interpreter=sub_interpreter,
                    node_outputs=scoped_outputs,
                    trigger_payload=trigger_payload,
                    inline_limit=inline_limit,
                    loop_vars=loop_vars,
                )

    async def _handle_node_error(
        self,
        *,
        run: WorkflowRun,
        node: NodeDefinition,
        nexec: NodeExecution,
        node_outputs: dict[str, dict[str, Any]],
        error: FlowPilotNodeError,
        side_effect: Any,
        retry_policy: Any,
        idempotency_key: str | None,
        ctx: ExecutionContext,
    ) -> bool:
        decision = evaluate_retry(
            side_effect=side_effect,
            policy=retry_policy,
            attempt=nexec.attempt,
            error_retryable=error.retryable,
            idempotency_key=idempotency_key,
            rng=self._rng,
        )
        redacted_msg = ctx.redact(error.message)
        if decision.should_retry:
            nexec.status = transition_node_status(nexec.status, NodeStatus.RETRYING)
            await self._emit(
                run,
                EventType.NODE_RETRYING,
                node_id=node.id,
                payload={
                    "attempt": nexec.attempt,
                    "delay_ms": decision.delay_ms,
                    "code": error.code,
                    "message": redacted_msg,
                },
            )
            await self._sleep(decision.delay_ms / 1000.0)
            return True

        detail = ErrorDetail(
            code=error.code,
            message=redacted_msg,
            retryable=error.retryable,
            node_id=node.id,
            details={
                **ctx.redact(error.details),
                "retry_stop_reason": decision.reason,
                "attempts": nexec.attempt,
            },
        )
        await self._fail_node(run, nexec, node_outputs, detail)
        return False

    async def _fail_node(
        self,
        run: WorkflowRun,
        nexec: NodeExecution,
        node_outputs: dict[str, dict[str, Any]],
        detail: ErrorDetail,
    ) -> None:
        if nexec.status == NodeStatus.READY:
            nexec.status = transition_node_status(nexec.status, NodeStatus.RUNNING)
        nexec.status = transition_node_status(nexec.status, NodeStatus.FAILED)
        nexec.finished_at = utc_now()
        nexec.error = detail
        node_outputs[nexec.node_id] = {"output": None, "error": detail.model_dump(mode="json")}
        await self._emit(
            run,
            EventType.NODE_FAILED,
            node_id=nexec.node_id,
            payload={"error": detail.model_dump(mode="json")},
        )

    async def _flush_ctx_telemetry(
        self,
        run: WorkflowRun,
        node_id: str,
        ctx: ExecutionContext,
        *,
        duration_ms: int,
    ) -> None:
        for entry in ctx.logs:
            await self._emit(
                run,
                EventType.LOG,
                node_id=node_id,
                payload={
                    "level": entry.level,
                    "message": entry.message,
                    "fields": entry.fields,
                },
            )
        run.usage_records.append(
            UsageRecord(
                organization_id=run.organization_id,
                run_id=run.id,
                node_id=node_id,
                metric="node_execution",
                quantity=1.0,
                unit="execution",
                metadata={"duration_ms": duration_ms},
            )
        )
        for rec in ctx.usage_records:
            run.usage_records.append(rec)
            await self._emit(
                run,
                EventType.USAGE_RECORDED,
                node_id=node_id,
                payload={"metric": rec.metric, "quantity": rec.quantity, "unit": rec.unit},
            )

    def _build_error_context(
        self,
        node_id: str,
        interpreter: GraphInterpreter,
        executions: dict[str, NodeExecution],
    ) -> dict[str, Any] | None:
        for edge in interpreter.incoming_edges(node_id):
            if edge.kind == EdgeKind.ERROR:
                src = executions[edge.source]
                if src.status == NodeStatus.FAILED and src.error is not None:
                    return src.error.model_dump(mode="json")
        return None

    def _find_latest_approval_for_node(
        self,
        run: WorkflowRun,
        node_id: str,
    ) -> ApprovalRequest | None:
        matches = [a for a in run.approvals.values() if a.node_id == node_id]
        if not matches:
            return None
        return matches[-1]

    def _compute_run_output(
        self,
        *,
        definition_output_mapping: dict[str, Any] | None,
        node_outputs: dict[str, dict[str, Any]],
        trigger_payload: dict[str, Any],
        run: WorkflowRun,
    ) -> dict[str, Any]:
        if definition_output_mapping:
            ctx = {
                "nodes": node_outputs,
                "trigger": trigger_payload,
                "inputs": trigger_payload,
                "env": self._env,
                "run": {"id": str(run.id), "organization_id": str(run.organization_id)},
            }
            resolved = resolve_value(definition_output_mapping, ctx)
            return resolved if isinstance(resolved, dict) else {"result": resolved}
        return {
            nid: data.get("output")
            for nid, data in node_outputs.items()
            if run.node_executions[nid].status == NodeStatus.SUCCEEDED
        }

    async def _emit(
        self,
        run: WorkflowRun,
        event_type: EventType,
        *,
        node_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        seq = self._event_seq.get(run.id, 0) + 1
        self._event_seq[run.id] = seq
        event = ExecutionEvent(
            sequence=seq,
            run_id=run.id,
            organization_id=run.organization_id,
            event_type=event_type,
            node_id=node_id,
            payload=payload or {},
        )
        await self._event_sink.emit(event)
