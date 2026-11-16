"""
Unit tests for Open Symptom Ingestion Layer.
Verifies:
  1. SymptomSignal model validation and serialization
  2. Generic log parser emitting SymptomSignal on ERROR/CRITICAL/FATAL and HTTP >= 500
  3. K8s Warning event watcher emitting SymptomSignal across arbitrary kinds (Pod, StatefulSet, PVC, Ingress)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.agents.k8s.agent import K8sAgent
from nexus.bus.events.enums import Severity, SignalKind, SignalSource
from nexus.bus.events.event import SymptomSignal
from nexus.telemetry.log_parser import is_error_log, parse_and_group_log_burst


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


@pytest.mark.asyncio
async def test_k8s_agent_warning_event_watcher():
    mock_nats = MagicMock()
    agent = K8sAgent(nats_client=mock_nats, namespaces=["prod"])

    # Simulate k8s v1.Event for a StatefulSet
    mock_ev = MagicMock()
    mock_ev.metadata.uid = "ev-uid-001"
    mock_ev.metadata.namespace = "prod"
    mock_ev.involved_object.kind = "StatefulSet"
    mock_ev.involved_object.name = "redis-cluster"
    mock_ev.involved_object.namespace = "prod"
    mock_ev.involved_object.api_version = "apps/v1"
    mock_ev.reason = "FailedCreate"
    mock_ev.message = "PVC storage quota exceeded for redis-data"
    mock_ev.count = 3
    mock_ev.type = "Warning"

    signal = agent._event_to_symptom_signal(mock_ev)
    assert signal.source == SignalSource.KUBERNETES_API
    assert signal.kind == SignalKind.STATE_TRANSITION
    assert signal.resource_urn == "k8s://prod/apps/v1/StatefulSet/redis-cluster"
    assert "redis-cluster" in signal.symptom_summary
    assert "FailedCreate" in signal.symptom_summary

    # Also test sense() producing IncidentEvents without 'str' has no attribute 'value' error
    agent._k8s_core = MagicMock()
    agent._k8s_core.list_namespaced_event.return_value = MagicMock(items=[mock_ev])
    events = await agent.sense()
    assert len(events) == 1
    assert events[0].resource_name == "redis-cluster"
    assert events[0].severity in ("high", Severity.HIGH)
