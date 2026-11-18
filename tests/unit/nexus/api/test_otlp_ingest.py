"""
Unit Tests for Plane 1 & 2: Pure OTLP Ingestion, involvedObject Extraction,
Correlation with None Identity, and NATS JetStream Deduplication.
"""

from __future__ import annotations

import gzip
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nexus.api.ingest import (
    _attrs_list_to_dict,
    extract_symptom_signals_from_otlp,
    extract_telemetry_context,
)
from nexus.bus.nats.client import NATSClient
from nexus.observability.status_api import app
from nexus.reasoning.event_correlator import EventCorrelator

client = TestClient(app)


def test_attrs_list_to_dict_handles_all_otlp_types():
    attrs = [
        {"key": "k8s.namespace.name", "value": {"stringValue": "shop-ns"}},
        {"key": "k8s.pod.restarts", "value": {"intValue": 3}},
        {"key": "nexus.enabled", "value": {"boolValue": True}},
        {
            "key": "nested",
            "value": {
                "kvlistValue": {"values": [{"key": "a", "value": {"stringValue": "b"}}]}
            },
        },
    ]
    parsed = _attrs_list_to_dict(attrs)
    assert parsed == {
        "k8s.namespace.name": "shop-ns",
        "k8s.pod.restarts": 3,
        "nexus.enabled": True,
        "nested": {"a": "b"},
    }


def test_extract_telemetry_context_hoists_involved_object_and_never_fabricates_defaults():
    """Missing namespace/service_name stay None; never invents 'default' or 'unknown-service'."""
    body = {
        "object": {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": "Readiness probe failed on port 8001",
            "involvedObject": {
                "kind": "Pod",
                "name": "shop-demo-59bd7ddcf9-t2skj",
                "namespace": "shop-ns",
            },
        }
    }
    ctx = extract_telemetry_context(resource_attrs={}, log_attrs={}, body=body)
    assert ctx["platform"] == "k8s"
    assert ctx["namespace"] == "shop-ns"
    assert ctx["service_name"] is None
    assert ctx["involved_object_name"] == "shop-demo-59bd7ddcf9-t2skj"
    assert ctx["involved_object_kind"] == "Pod"

    # Completely bare event with no namespace/service metadata
    bare = extract_telemetry_context(
        resource_attrs={}, log_attrs={}, body="bare log line"
    )
    assert bare["platform"] is None
    assert bare["namespace"] is None
    assert bare["service_name"] is None
    assert bare["involved_object_name"] is None


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
    assert sig.involved_object_name == "checkout-svc-789"
    assert sig.reason == "OOMKilled"
    assert "OOMKilled" in sig.message


def test_correlator_with_none_identity_uses_involved_object_name():
    """Distinct pods with namespace=None and service_name=None do NOT merge into a single bucket."""
    otlp_a = {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "severityText": "WARN",
                                "body": {
                                    "object": {
                                        "type": "Warning",
                                        "reason": "Unhealthy",
                                        "message": "Readiness probe failed",
                                        "involvedObject": {
                                            "kind": "Pod",
                                            "name": "shop-demo-59bd7ddcf9-aaaa1",
                                        },
                                    }
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    otlp_b = {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "severityText": "WARN",
                                "body": {
                                    "object": {
                                        "type": "Warning",
                                        "reason": "Unhealthy",
                                        "message": "Readiness probe failed",
                                        "involvedObject": {
                                            "kind": "Pod",
                                            "name": "payments-api-77aa12bc-bbbb2",
                                        },
                                    }
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }

    sig_a = extract_symptom_signals_from_otlp(otlp_a)[0]
    sig_b = extract_symptom_signals_from_otlp(otlp_b)[0]
    assert sig_a.region_or_namespace is None
    assert sig_a.service_name is None
    assert sig_a.involved_object_name == "shop-demo-59bd7ddcf9-aaaa1"

    correlator = EventCorrelator(quorum_events=2)
    ev_a = sig_a.to_incident_event()
    ev_b = sig_b.to_incident_event()

    assert correlator._cluster_key(ev_a) != correlator._cluster_key(ev_b)
    assert "shop-demo-59bd7ddcf9-aaaa1" in correlator._cluster_key(ev_a)
    assert "payments-api-77aa12bc-bbbb2" in correlator._cluster_key(ev_b)

    assert correlator.ingest(ev_a) is None
    # Ingesting ev_b must open a separate cluster, not reach quorum on ev_a's cluster
    assert correlator.ingest(ev_b) is None
    assert correlator.stats["open_clusters"] == 2


@pytest.mark.asyncio
async def test_otlp_webhook_endpoints_publish_to_nats_with_msg_id():
    """Verify POST /api/v1/ingest/otlp and /api/v1/ingest/otlp/events publish with Nats-Msg-Id."""
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
        resp_primary = client.post("/api/v1/ingest/otlp", json=otlp_payload)
        resp_events = client.post("/api/v1/ingest/otlp/events", json=otlp_payload)

    assert resp_primary.status_code == 200
    assert resp_primary.json() == {"status": "accepted", "signals_processed": 1}
    assert resp_events.status_code == 200
    assert resp_events.json() == {"status": "accepted", "signals_processed": 1}

    assert mock_nats.publish.call_count == 2
    kwargs = mock_nats.publish.call_args.kwargs
    assert kwargs["subject"] == "nexus.incidents.raw"
    assert len(kwargs["headers"]["Nats-Msg-Id"]) == 64


@pytest.mark.asyncio
async def test_otlp_webhook_handles_otlphttp_subpaths_protobuf_and_gzip():
    """Verify protobuf and gzip decompression on /api/v1/ingest/otlp/events/v1/logs and /v1/logs."""
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

    otlp_json = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "k8s.pod.name",
                            "value": {"stringValue": "shop-demo-123"},
                        }
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {
                                    "stringValue": "Back-off restarting failed container"
                                },
                                "severityText": "WARN",
                            }
                        ]
                    }
                ],
            }
        ]
    }
    gzipped_bytes = gzip.compress(json.dumps(otlp_json).encode("utf-8"))

    mock_nats = MagicMock(spec=NATSClient)
    mock_nats.is_connected = True
    mock_nats.publish = AsyncMock(return_value=True)

    with patch.object(NATSClient, "get_instance", return_value=mock_nats):
        resp1 = client.post(
            "/api/v1/ingest/otlp/events/v1/logs",
            content=proto_bytes,
            headers={"Content-Type": "application/x-protobuf"},
        )
        resp2 = client.post(
            "/v1/logs",
            content=proto_bytes,
            headers={"Content-Type": "application/x-protobuf"},
        )
        resp3 = client.post(
            "/api/v1/ingest/otlp/v1/logs",
            content=gzipped_bytes,
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )

    assert resp1.status_code == 200 and resp1.json()["signals_processed"] == 1
    assert resp2.status_code == 200 and resp2.json()["signals_processed"] == 1
    assert resp3.status_code == 200 and resp3.json()["signals_processed"] == 1
