"""
OpenTelemetry Logging Setup for Telemetry Pipeline
"""

from __future__ import annotations

import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource


def setup_nginx_otel_logger(otel_endpoint: str) -> logging.Logger:
    """Configure OTel SDK to export logs to Collector via OTLP/HTTP."""
    resource = Resource.create(
        {
            "service.name": "nexus-nginx-log-shipper",
            "service.version": "0.1.0",
            "service.namespace": "nexus",
        }
    )
    exporter = OTLPLogExporter(endpoint=f"{otel_endpoint}/v1/logs")
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    handler = LoggingHandler(level=logging.DEBUG, logger_provider=provider)
    otel_logger = logging.getLogger("nexus.nginx")
    otel_logger.addHandler(handler)
    otel_logger.setLevel(logging.DEBUG)
    return otel_logger
