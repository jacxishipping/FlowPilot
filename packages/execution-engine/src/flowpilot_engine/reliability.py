"""Circuit breakers, Dead-Letter Queue (DLQ), and live EventBus (in-memory & Redis Streams)."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from flowpilot_schema import ErrorDetail, ExecutionEvent


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a target integration circuit breaker is OPEN."""

    def __init__(self, key: str, retry_after_seconds: float) -> None:
        super().__init__(
            f"Circuit breaker '{key}' is OPEN; retry after {retry_after_seconds:.1f}s."
        )
        self.key = key
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """Three-state circuit breaker protecting external integrations from cascading failures."""

    def __init__(
        self,
        key: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.key = key
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._half_open_max_calls = half_open_max_calls
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow_request(self, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.monotonic()
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            assert self._opened_at is not None
            if (ts - self._opened_at) >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = 1
                return True
            return False
        if self._half_open_in_flight < self._half_open_max_calls:
            self._half_open_in_flight += 1
            return True
        return False

    def ensure_allowed(self, *, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        if not self.allow_request(now=ts):
            remaining = 0.0
            if self._opened_at is not None:
                remaining = max(0.0, self._recovery_timeout - (ts - self._opened_at))
            raise CircuitOpenError(self.key, remaining)

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._half_open_in_flight = 0

    def record_failure(self, *, now: float | None = None) -> None:
        ts = now if now is not None else time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = ts
            self._half_open_in_flight = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = ts


class CircuitBreakerRegistry:
    """Tenant- and integration-scoped registry of circuit breakers."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout_seconds
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, key: str) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                key,
                failure_threshold=self._failure_threshold,
                recovery_timeout_seconds=self._recovery_timeout,
            )
            self._breakers[key] = breaker
        return breaker


class DeadLetterEntry(BaseModel):
    """Terminal failed node/run payload parked for inspection and safe manual replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    run_id: UUID
    workflow_id: UUID
    workflow_version_id: UUID
    node_id: str | None = None
    error: ErrorDetail
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    replayed: bool = False


class DeadLetterQueue(Protocol):
    async def enqueue(self, entry: DeadLetterEntry) -> None: ...
    async def list_for_org(self, organization_id: UUID) -> list[DeadLetterEntry]: ...


class InMemoryDeadLetterQueue:
    def __init__(self) -> None:
        self._entries: list[DeadLetterEntry] = []

    async def enqueue(self, entry: DeadLetterEntry) -> None:
        self._entries.append(entry)

    async def list_for_org(self, organization_id: UUID) -> list[DeadLetterEntry]:
        return [e for e in self._entries if e.organization_id == organization_id]


class EventBus(Protocol):
    """Pub-sub transport delivering live ExecutionEvents to SSE subscribers."""

    async def publish(self, event: ExecutionEvent) -> None: ...
    def subscribe(self, run_id: UUID) -> AsyncIterator[ExecutionEvent]: ...


class InMemoryEventBus:
    """Zero-polling asyncio queue event bus for live SSE streaming."""

    def __init__(self) -> None:
        self._subscribers: dict[UUID, list[asyncio.Queue[ExecutionEvent]]] = defaultdict(list)

    async def publish(self, event: ExecutionEvent) -> None:
        for queue in list(self._subscribers.get(event.run_id, ())):
            await queue.put(event)

    async def subscribe(self, run_id: UUID) -> AsyncIterator[ExecutionEvent]:
        queue: asyncio.Queue[ExecutionEvent] = asyncio.Queue()
        self._subscribers[run_id].append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            if queue in self._subscribers.get(run_id, ()):
                self._subscribers[run_id].remove(queue)


class RedisStreamsEventBus:
    """Redis Streams event bus for multi-replica live SSE fan-out."""

    def __init__(self, redis_client: Any, *, stream_prefix: str = "flowpilot:run_events") -> None:
        self._redis = redis_client
        self._prefix = stream_prefix

    def _stream_key(self, run_id: UUID) -> str:
        return f"{self._prefix}:{run_id}"

    async def publish(self, event: ExecutionEvent) -> None:
        payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
        await self._redis.xadd(
            self._stream_key(event.run_id),
            {"event": payload, "seq": str(event.sequence)},
            maxlen=5000,
            approximate=True,
        )

    async def subscribe(
        self,
        run_id: UUID,
        *,
        last_id: str = "$",
        block_ms: int = 1000,
    ) -> AsyncIterator[ExecutionEvent]:
        cursor = last_id
        key = self._stream_key(run_id)
        while True:
            entries = await self._redis.xread({key: cursor}, count=50, block=block_ms)
            if not entries:
                continue
            for _stream, messages in entries:
                for msg_id, fields in messages:
                    cursor = msg_id
                    raw = fields.get("event") or fields.get(b"event")
                    if raw is not None:
                        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        yield ExecutionEvent.model_validate_json(text)
