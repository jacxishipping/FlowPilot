"""Standard 5-field cron matcher and idempotent trigger scheduler."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flowpilot_api.db.models import WorkflowModel, WorkflowTriggerModel, WorkflowVersionModel
from flowpilot_engine import WorkflowExecutionEngine
from flowpilot_schema import WorkflowDefinition, WorkflowVersion


def _match_field(expr: str, value: int, *, minimum: int, maximum: int) -> bool:
    for part in expr.split(","):
        token = part.strip()
        if token == "*":
            return True
        if token.startswith("*/"):
            step = int(token[2:])
            if step <= 0:
                raise ValueError(f"Invalid cron step '{token}'.")
            if (value - minimum) % step == 0:
                return True
            continue
        if "-" in token:
            lo_str, hi_str = token.split("-", 1)
            lo, hi = int(lo_str), int(hi_str)
            if lo < minimum or hi > maximum:
                raise ValueError(f"Cron range '{token}' out of bounds [{minimum}, {maximum}].")
            if lo <= value <= hi:
                return True
            continue
        num = int(token)
        if num < minimum or num > maximum:
            raise ValueError(f"Cron value '{num}' out of bounds [{minimum}, {maximum}].")
        if num == value:
            return True
    return False


def cron_matches(cron_expression: str, dt: datetime) -> bool:
    """Return True if a 5-field cron expression (min hour dom month dow) matches UTC datetime."""
    fields = cron_expression.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"Expected 5-field cron expression, got {len(fields)} fields: '{cron_expression}'."
        )
    utc_dt = dt.astimezone(UTC)
    # Cron weekday: 0=Sunday .. 6=Saturday
    cron_dow = (utc_dt.weekday() + 1) % 7
    return (
        _match_field(fields[0], utc_dt.minute, minimum=0, maximum=59)
        and _match_field(fields[1], utc_dt.hour, minimum=0, maximum=23)
        and _match_field(fields[2], utc_dt.day, minimum=1, maximum=31)
        and _match_field(fields[3], utc_dt.month, minimum=1, maximum=12)
        and _match_field(fields[4], cron_dow, minimum=0, maximum=6)
    )


class TriggerScheduler:
    """Evaluates enabled schedule triggers and dispatches pinned runs with bucket idempotency."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        engine: WorkflowExecutionEngine,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine

    async def tick(self, *, now: datetime | None = None) -> list[UUID]:
        """Evaluate all enabled 'schedule' triggers for the given minute bucket."""
        ts = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
        bucket_key = ts.strftime("%Y%m%d%H%M")
        dispatched_run_ids: list[UUID] = []

        async with self._session_factory() as session:
            stmt = select(WorkflowTriggerModel).where(
                WorkflowTriggerModel.enabled.is_(True),
                WorkflowTriggerModel.trigger_type == "schedule",
            )
            triggers = (await session.execute(stmt)).scalars().all()

            for trig in triggers:
                cron_expr = str(trig.config.get("cron", "")).strip()
                if not cron_expr or not cron_matches(cron_expr, ts):
                    continue

                wf = await session.get(WorkflowModel, trig.workflow_id)
                if wf is None or not wf.is_active or wf.active_version_id is None:
                    continue
                ver_row = await session.get(WorkflowVersionModel, wf.active_version_id)
                if ver_row is None:
                    continue

                version = WorkflowVersion(
                    id=ver_row.id,
                    workflow_id=ver_row.workflow_id,
                    organization_id=ver_row.organization_id,
                    version_number=ver_row.version_number,
                    definition=WorkflowDefinition.model_validate(ver_row.definition_json),
                    content_hash=ver_row.content_hash,
                    published_at=ver_row.published_at,
                    published_by=ver_row.published_by,
                    changelog=ver_row.changelog,
                )

                # Deterministic per-minute idempotency key guarantees restarts never double-fire
                idem_key = f"sched:{trig.id}:{bucket_key}"
                run = await self._engine.start_run(
                    version,
                    organization_id=wf.organization_id,
                    trigger_input={"scheduled_at": ts.isoformat(), "trigger_id": str(trig.id)},
                    trigger_type="schedule",
                    idempotency_key=idem_key,
                )
                dispatched_run_ids.append(run.id)

        return dispatched_run_ids
