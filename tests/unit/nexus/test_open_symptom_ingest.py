"""
Unit tests for Open Symptom Ingestion Layer.
Verifies:
  1. SymptomSignal model validation and serialization
  2. Generic log parser emitting SymptomSignal on ERROR/CRITICAL/FATAL and HTTP >= 500
"""

from __future__ import annotations

from nexus.core.bus.events.enums import Severity, SignalKind, SignalSource
from nexus.core.bus.events.event import SymptomSignal
from nexus.core.telemetry.log_parser import is_error_log, parse_and_group_log_burst


def test_symptom_signal_creation_and_defaults():
    sig = SymptomSignal(
        source=SignalSource.AWS_CLOUDWATCH,
        kind=SignalKind.METRIC_ANOMALY,
        provider="aws",
        resource_urn="arn:aws:rds:us-east-1:123456789012:db:prod-pg",
        region_or_namespace="us-east-1",
        symptom_summary="Database connection pool exhausted",
        raw_payload={"active_connections": 105, "max_connections": 100},
    )
    assert sig.signal_id.startswith("sig-")
    assert sig.source == SignalSource.AWS_CLOUDWATCH
    assert sig.kind == SignalKind.METRIC_ANOMALY
    assert sig.severity == Severity.HIGH
    assert sig.provider == "aws"
    assert "rds" in sig.resource_urn

    # NATS payload serialization
    payload = sig.to_nats_payload()
    restored = SymptomSignal.from_nats_payload(payload)
    assert restored.signal_id == sig.signal_id
    assert restored.resource_urn == sig.resource_urn


def test_log_parser_burst_detection():
    logs = [
        "2026-11-15 10:00:00 INFO Service started normally",
        "2026-11-15 10:00:01 WARNING High latency observed",
        "2026-11-15 10:00:02 ERROR Unhandled exception in worker thread",
        "2026-11-15 10:00:03 CRITICAL Database connection dropped",
    ]
    assert is_error_log(logs[0]) is False
    assert is_error_log(logs[2]) is True
    assert is_error_log(logs[3]) is True

    # NGINX line with status >= 500
    nginx_err = '127.0.0.1 - - [15/Nov/2026:10:00:05 +0000] "GET /api/pay HTTP/1.1" 503 124 "-" "curl/7.68.0"'
    assert is_error_log(nginx_err) is True

    signal = parse_and_group_log_burst(
        logs=logs,
        resource_urn="k8s://prod/apps/v1/Deployment/order-service",
        provider="kubernetes",
        region_or_namespace="prod",
    )
    assert signal is not None
    assert signal.kind == SignalKind.ERROR_LOG_BURST
    assert signal.raw_payload["total_error_count"] == 2
    assert "order-service" in signal.symptom_summary
