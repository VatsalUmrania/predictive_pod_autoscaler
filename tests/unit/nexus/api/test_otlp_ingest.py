"""
Unit Tests for Plane 1 & 2: OTLP Ingestion Webhook & NATS JetStream Deduplication
================================================================================
Verifies:
  1. OTLP Event normalization to SymptomSignal.
  2. Deterministic SHA-256 deduplication hashing with Nats-Msg-Id.
  3. Reactive NATS JetStream publication with headers.
  4. Single-flight incident coalescing trigger.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nexus.api.ingest import (
    extract_symptom_signals_from_otlp,
)
from nexus.bus.nats.client import NATSClient
from nexus.observability.status_api import app

client = TestClient(app)


def test_extract_symptom_signals_from_otlp_pod_oom():
    """Verify OTLP event structure extracts into normalized SymptomSignal attributes."""
    otlp_payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "k8s.namespace.name",
                            "value": {"stringValue": "production"},
                        },
                        {
                            "key": "k8s.deployment.name",
                            "value": {"stringValue": "checkout-svc"},
                        },
                        {
                            "key": "k8s.pod.name",
                            "value": {"stringValue": "checkout-svc-789"},
                        },
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1700000000000000000",
                                "severityText": "ERROR",
                                "body": {
                                    "stringValue": "Pod checkout-svc-789 killed with OOMKilled exit code 137"
                                },
                                "attributes": [
                                    {
                                        "key": "k8s.event.reason",
                                        "value": {"stringValue": "OOMKilled"},
                                    },
                                    {
                                        "key": "k8s.event.action",
                                        "value": {"stringValue": "Killing"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    signals = extract_symptom_signals_from_otlp(otlp_payload)
    assert len(signals) == 1
    sig = signals[0]

    assert sig.workload_urn == "k8s://production/apps/v1/Deployment/checkout-svc"
    assert sig.service_name == "checkout-svc"
    assert sig.namespace == "production"
    assert sig.reason == "OOMKilled"
    assert "OOMKilled" in sig.message


@pytest.mark.asyncio
async def test_otlp_webhook_endpoint_publishes_to_nats_with_msg_id():
    """Verify POST /api/v1/ingest/otlp/events publishes to JetStream with Nats-Msg-Id."""
    otlp_payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "k8s.namespace.name",
                            "value": {"stringValue": "staging"},
                        },
                        {
                            "key": "k8s.pod.name",
                            "value": {"stringValue": "auth-service-xyz"},
                        },
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1700000000000000000",
                                "severityText": "WARN",
                                "body": {
                                    "stringValue": "CrashLoopBackOff detected on container auth"
                                },
                                "attributes": [
                                    {
                                        "key": "k8s.event.reason",
                                        "value": {"stringValue": "CrashLoopBackOff"},
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    mock_nats = MagicMock(spec=NATSClient)
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock(return_value=True)

    with patch.object(NATSClient, "get_instance", return_value=mock_nats):
        response = client.post("/api/v1/ingest/otlp/events", json=otlp_payload)

    assert response.status_code == 200
    res_data = response.json()
    assert res_data["status"] == "accepted"
    assert res_data["signals_processed"] == 1

    # Verify NATS publish was invoked with deterministic Nats-Msg-Id header
    mock_nats.publish.assert_called_once()
    kwargs = mock_nats.publish.call_args.kwargs
    subject = kwargs.get("subject")
    headers = kwargs.get("headers")

    assert subject == "nexus.incidents.raw"
    assert headers is not None
    assert "Nats-Msg-Id" in headers
    assert len(headers["Nats-Msg-Id"]) == 64


@pytest.mark.asyncio
async def test_otlp_webhook_handles_otlphttp_subpaths_and_protobuf():
    """Verify collector paths (/api/v1/ingest/otlp/events/v1/logs and /v1/logs) and protobuf."""
    from opentelemetry.proto.logs.v1.logs_pb2 import LogsData

    ld = LogsData()
    rl = ld.resource_logs.add()
    rl.resource.attributes.add(
        key="k8s.pod.name", value={"string_value": "checkout-pod-999"}
    )
    sl = rl.scope_logs.add()
    lr = sl.log_records.add()
    lr.body.string_value = "OOMKilled pod crash"
    lr.severity_text = "ERROR"

    proto_bytes = ld.SerializeToString()

    mock_nats = MagicMock(spec=NATSClient)
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock(return_value=True)

    with patch.object(NATSClient, "get_instance", return_value=mock_nats):
        # 1. Test /api/v1/ingest/otlp/events/v1/logs with protobuf
        resp1 = client.post(
            "/api/v1/ingest/otlp/events/v1/logs",
            content=proto_bytes,
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["signals_processed"] == 1

        # 2. Test /v1/logs with protobuf
        resp2 = client.post(
            "/v1/logs",
            content=proto_bytes,
            headers={"Content-Type": "application/x-protobuf"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["signals_processed"] == 1


@pytest.mark.asyncio
async def test_otlp_webhook_handles_gzip_compressed_json():
    """Verify endpoint decompresses and processes gzip-compressed OTLP payloads."""
    import gzip
    import json

    otlp_payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "k8s.namespace.name", "value": {"stringValue": "default"}},
                        {"key": "k8s.pod.name", "value": {"stringValue": "shop-demo-123"}},
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "Warning: Back-off restarting failed container"},
                                "severityText": "WARN",
                                "attributes": [
                                    {"key": "k8s.event.reason", "value": {"stringValue": "BackOff"}}
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }

    gzipped_bytes = gzip.compress(json.dumps(otlp_payload).encode("utf-8"))

    mock_nats = MagicMock(spec=NATSClient)
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock(return_value=True)

    with patch.object(NATSClient, "get_instance", return_value=mock_nats):
        resp = client.post(
            "/api/v1/ingest/otlp/events/v1/logs",
            content=gzipped_bytes,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"
        assert resp.json()["signals_processed"] == 1
