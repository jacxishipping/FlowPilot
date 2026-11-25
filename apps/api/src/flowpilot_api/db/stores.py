"""SQLAlchemy-backed ExecutionStore, EventSink, and Envelope-Encrypted CredentialResolver."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flowpilot import CredentialError, ResolvedCredential, SecretValue
from flowpilot_api.db.models import (
    ApprovalModel,
    CredentialModel,
    ExecutionEventModel,
    NodeExecutionModel,
    UsageRecordModel,
    WorkflowRunModel,
    WorkflowVersionModel,
)
from flowpilot_api.db.session import set_tenant_rls_context
from flowpilot_credentials import EncryptedEnvelope, EnvelopeCipher
from flowpilot_schema import (
    EventType,
    ExecutionEvent,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowVersion,
)


class SqlAlchemyExecutionStore:
    """Persists pinned WorkflowVersions, WorkflowRuns, NodeExecutions, Approvals, and Usage."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save_version(self, version: WorkflowVersion) -> None:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, version.organization_id)
            existing = await session.get(WorkflowVersionModel, version.id)
            if existing is None:
                session.add(
                    WorkflowVersionModel(
                        id=version.id,
                        organization_id=version.organization_id,
                        workflow_id=version.workflow_id,
                        version_number=version.version_number,
                        definition_json=version.definition.model_dump(mode="json"),
                        content_hash=version.content_hash,
                        changelog=version.changelog,
                        published_by=version.published_by,
                        published_at=version.published_at,
                    )
                )
                await session.commit()

    async def get_version(self, version_id: UUID) -> WorkflowVersion | None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowVersionModel, version_id)
            if row is None:
                return None
            return WorkflowVersion(
                id=row.id,
                workflow_id=row.workflow_id,
                organization_id=row.organization_id,
                version_number=row.version_number,
                definition=WorkflowDefinition.model_validate(row.definition_json),
                content_hash=row.content_hash,
                published_at=row.published_at,
                published_by=row.published_by,
                changelog=row.changelog,
            )

    async def save_run(self, run: WorkflowRun) -> None:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, run.organization_id)
            payload = run.model_dump(mode="json")
            row = await session.get(WorkflowRunModel, run.id)
            if row is None:
                row = WorkflowRunModel(
                    id=run.id,
                    organization_id=run.organization_id,
                    workflow_id=run.workflow_id,
                    workflow_version_id=run.workflow_version_id,
                    status=run.status.value,
                    trigger_type=run.trigger_type,
                    idempotency_key=run.idempotency_key,
                    temporal_workflow_id=run.temporal_workflow_id,
                    temporal_run_id=run.temporal_run_id,
                    run_state_json=payload,
                    created_at=run.created_at,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
                session.add(row)
            else:
                row.status = run.status.value
                row.temporal_workflow_id = run.temporal_workflow_id
                row.temporal_run_id = run.temporal_run_id
                row.run_state_json = payload
                row.started_at = run.started_at
                row.finished_at = run.finished_at

            # Sync node_executions table for structured SQL querying
            for nexec in run.node_executions.values():
                nrow = await session.get(NodeExecutionModel, nexec.id)
                nstate = nexec.model_dump(mode="json")
                if nrow is None:
                    session.add(
                        NodeExecutionModel(
                            id=nexec.id,
                            organization_id=run.organization_id,
                            run_id=run.id,
                            node_id=nexec.node_id,
                            node_type=nexec.node_type,
                            status=nexec.status.value,
                            attempt=nexec.attempt,
                            duration_ms=nexec.duration_ms,
                            state_json=nstate,
                        )
                    )
                else:
                    nrow.status = nexec.status.value
                    nrow.attempt = nexec.attempt
                    nrow.duration_ms = nexec.duration_ms
                    nrow.state_json = nstate

            # Sync approvals table
            for approval in run.approvals.values():
                arow = await session.get(ApprovalModel, approval.id)
                if arow is None:
                    session.add(
                        ApprovalModel(
                            id=approval.id,
                            organization_id=approval.organization_id,
                            run_id=approval.run_id,
                            node_id=approval.node_id,
                            title=approval.title,
                            description=approval.description,
                            risk_level=approval.risk_level.value,
                            decision=approval.decision.value,
                            decided_by=approval.decided_by,
                            comment=approval.comment,
                            payload=approval.payload,
                            requested_at=approval.requested_at,
                            decided_at=approval.decided_at,
                        )
                    )
                else:
                    arow.decision = approval.decision.value
                    arow.decided_by = approval.decided_by
                    arow.comment = approval.comment
                    arow.decided_at = approval.decided_at

            # Append immutable usage records not yet stored
            for rec in run.usage_records:
                urow = await session.get(UsageRecordModel, rec.id)
                if urow is None:
                    session.add(
                        UsageRecordModel(
                            id=rec.id,
                            organization_id=rec.organization_id,
                            run_id=rec.run_id,
                            node_id=rec.node_id,
                            metric=rec.metric,
                            quantity=rec.quantity,
                            unit=rec.unit,
                            metadata_json=rec.metadata,
                            recorded_at=rec.recorded_at,
                        )
                    )

            await session.commit()

    async def get_run(self, run_id: UUID) -> WorkflowRun | None:
        async with self._session_factory() as session:
            row = await session.get(WorkflowRunModel, run_id)
            if row is None:
                return None
            return WorkflowRun.model_validate(row.run_state_json)

    async def find_run_by_idempotency_key(
        self,
        *,
        organization_id: UUID,
        workflow_id: UUID,
        idempotency_key: str,
    ) -> WorkflowRun | None:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, organization_id)
            stmt = select(WorkflowRunModel).where(
                WorkflowRunModel.organization_id == organization_id,
                WorkflowRunModel.workflow_id == workflow_id,
                WorkflowRunModel.idempotency_key == idempotency_key,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return WorkflowRun.model_validate(row.run_state_json)


class SqlAlchemyEventSink:
    """Persists ordered ExecutionEvents to PostgreSQL and fans out live via EventBus."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        event_bus: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._event_bus = event_bus

    async def emit(self, event: ExecutionEvent) -> None:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, event.organization_id)
            session.add(
                ExecutionEventModel(
                    id=event.id,
                    organization_id=event.organization_id,
                    run_id=event.run_id,
                    sequence=event.sequence,
                    event_type=event.event_type.value,
                    node_id=event.node_id,
                    payload=event.payload,
                    timestamp=event.timestamp,
                )
            )
            await session.commit()
        if self._event_bus is not None:
            await self._event_bus.publish(event)

    async def list_events(
        self,
        run_id: UUID,
        *,
        organization_id: UUID,
        after_sequence: int = 0,
    ) -> list[ExecutionEvent]:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, organization_id)
            stmt = (
                select(ExecutionEventModel)
                .where(
                    ExecutionEventModel.run_id == run_id,
                    ExecutionEventModel.organization_id == organization_id,
                    ExecutionEventModel.sequence > after_sequence,
                )
                .order_by(ExecutionEventModel.sequence.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                ExecutionEvent(
                    id=r.id,
                    sequence=r.sequence,
                    run_id=r.run_id,
                    organization_id=r.organization_id,
                    event_type=EventType(r.event_type),
                    node_id=r.node_id,
                    timestamp=r.timestamp,
                    payload=r.payload,
                )
                for r in rows
            ]


class SqlAlchemyCredentialResolver:
    """Decrypts envelope-encrypted tenant credentials on demand for node execution."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: EnvelopeCipher,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def resolve(
        self,
        *,
        organization_id: UUID,
        credential_id: UUID,
    ) -> ResolvedCredential:
        async with self._session_factory() as session:
            await set_tenant_rls_context(session, organization_id)
            row = await session.get(CredentialModel, credential_id)
            if row is None or row.organization_id != organization_id:
                raise CredentialError(
                    f"Credential '{credential_id}' not found for organization '{organization_id}'."
                )
            envelope = EncryptedEnvelope.model_validate(row.encrypted_envelope)
            plaintext_map = self._cipher.decrypt_secrets(
                envelope,
                organization_id=organization_id,
                credential_id=credential_id,
            )
            return ResolvedCredential(
                credential_id=row.id,
                organization_id=row.organization_id,
                credential_type=row.credential_type,
                secrets={k: SecretValue(v) for k, v in plaintext_map.items()},
                metadata=row.metadata_json,
                scopes=tuple(row.scopes),
            )
