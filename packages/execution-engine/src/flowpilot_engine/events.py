"""Execution event stream abstractions and SSE formatter."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Protocol
from uuid import UUID

from flowpilot_schema import ExecutionEvent


class EventSink(Protocol):
    """Sink receiving ordered execution events for audit trails and real-time UI streaming."""

    async def emit(self, event: ExecutionEvent) -> None: ...


class InMemoryEventSink:
    """In-memory event sink useful for local runs, SSE demos, and automated tests."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[ExecutionEvent]] = defaultdict(list)

    async def emit(self, event: ExecutionEvent) -> None:
        self._events[event.run_id].append(event)

    def get_events(self, run_id: UUID) -> list[ExecutionEvent]:
        return list(self._events.get(run_id, ()))


def format_sse(event: ExecutionEvent) -> str:
    """Format an ExecutionEvent as a Server-Sent Events (SSE) frame."""
    data = json.dumps(event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False)
    return f"id: {event.sequence}\nevent: {event.event_type.value}\ndata: {data}\n\n"
