"""OpenTelemetry OTLP exporter configuration helper."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("flowpilot.observability")


@dataclass(frozen=True)
class OtlpConfig:
    service_name: str = "flowpilot-api"
    otlp_endpoint: str | None = None
    enabled: bool = False


def configure_otlp_tracing(config: OtlpConfig) -> bool:
    """Configure OpenTelemetry SDK + OTLP HTTP/gRPC span exporter when installed & enabled.

    Returns True if an OTLP SDK provider was initialized, False if running with no-op tracer.
    """
    if not config.enabled or not config.otlp_endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTLP enabled (%s) but opentelemetry-sdk / exporter-otlp not installed; "
            "using default tracer.",
            config.otlp_endpoint,
        )
        return False

    resource = Resource.create({"service.name": config.service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return True
