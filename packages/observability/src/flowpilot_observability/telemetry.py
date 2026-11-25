"""OpenTelemetry tracing helpers and lightweight Prometheus-text metrics registry."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span


def get_tracer(service_name: str = "flowpilot") -> trace.Tracer:
    return trace.get_tracer(service_name)


@contextmanager
def trace_workflow_run(
    *,
    organization_id: str,
    workflow_id: str,
    workflow_version_id: str,
    run_id: str,
) -> Iterator[Span]:
    tracer = get_tracer("flowpilot.engine")
    with tracer.start_as_current_span("flowpilot.workflow.run") as span:
        span.set_attribute("flowpilot.organization_id", organization_id)
        span.set_attribute("flowpilot.workflow_id", workflow_id)
        span.set_attribute("flowpilot.workflow_version_id", workflow_version_id)
        span.set_attribute("flowpilot.run_id", run_id)
        yield span


@contextmanager
def trace_node_execution(
    *,
    run_id: str,
    node_id: str,
    node_type: str,
    worker_pool: str,
    attempt: int,
) -> Iterator[Span]:
    tracer = get_tracer("flowpilot.worker")
    with tracer.start_as_current_span(f"flowpilot.node.{node_type}") as span:
        span.set_attribute("flowpilot.run_id", run_id)
        span.set_attribute("flowpilot.node_id", node_id)
        span.set_attribute("flowpilot.node_type", node_type)
        span.set_attribute("flowpilot.worker_pool", worker_pool)
        span.set_attribute("flowpilot.attempt", attempt)
        yield span


class MetricsRegistry:
    """In-process counter/histogram metrics registry exporting Prometheus exposition format."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._counters[key] += value

    def get(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        return self._counters.get(key, 0.0)

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for (name, label_items), value in sorted(self._counters.items()):
            if label_items:
                formatted = ",".join(f'{k}="{v}"' for k, v in label_items)
                lines.append(f"{name}{{{formatted}}} {value}")
            else:
                lines.append(f"{name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")


def format_structured_log(
    level: str,
    message: str,
    *,
    request_id: str | None = None,
    organization_id: str | None = None,
    run_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Emit correlation-enriched JSON log records."""
    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": level.upper(),
        "message": message,
    }
    if request_id:
        record["request_id"] = request_id
    if organization_id:
        record["organization_id"] = organization_id
    if run_id:
        record["run_id"] = run_id
    if extra:
        record["extra"] = extra
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return format_structured_log(
            record.levelname,
            record.getMessage(),
            request_id=getattr(record, "request_id", None),
            organization_id=getattr(record, "organization_id", None),
            run_id=getattr(record, "run_id", None),
        )
