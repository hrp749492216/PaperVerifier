"""OpenTelemetry tracing setup for distributed observability.

Provides span creation helpers for LLM calls, external API requests,
and the verification pipeline. When no OTel exporter is configured,
spans are no-ops so there is zero overhead.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

_TRACER_NAME = "paperverifier"

_initialized = False


def setup_tracing() -> None:
    """Initialize OpenTelemetry with a console exporter.

    In production, replace ``ConsoleSpanExporter`` with an OTLP exporter
    by setting ``OTEL_EXPORTER_OTLP_ENDPOINT``.

    Safe to call multiple times; only the first call configures the provider.
    """
    global _initialized  # noqa: PLW0603
    if _initialized:
        return
    _initialized = True

    resource = Resource.create({"service.name": "paperverifier"})
    provider = TracerProvider(resource=resource)

    # Use console exporter by default; replace with OTLP in production.
    exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)


def get_tracer() -> trace.Tracer:
    """Return the application tracer."""
    return trace.get_tracer(_TRACER_NAME)
