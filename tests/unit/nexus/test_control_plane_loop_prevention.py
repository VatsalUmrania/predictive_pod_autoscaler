"""Tests for control plane self-healing loop prevention."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.graph.workflow_events import handle_incoming_event, is_control_plane_resource


def test_is_control_plane_resource():
    """Verify is_control_plane_resource identifies nexus infra and allows workloads."""
    # Control plane namespaces
    ev_nexus = SimpleNamespace(namespace="nexus", resource_name="some-service")
    assert is_control_plane_resource(ev_nexus) is True

    ev_k8s = SimpleNamespace(namespace="kube-system", resource_name="coredns")
    assert is_control_plane_resource(ev_k8s) is True

    ev_pub = SimpleNamespace(namespace="kube-public", resource_name="cluster-info")
    assert is_control_plane_resource(ev_pub) is True

    # Control plane pod/service prefixes
    ev_api = SimpleNamespace(
        namespace="default", resource_name="nexus-api-6f555b8cb7-8pcdv"
    )
    assert is_control_plane_resource(ev_api) is True

    ev_nats = SimpleNamespace(namespace="default", resource_name="nats-0")
    assert is_control_plane_resource(ev_nats) is True

    ev_opa = SimpleNamespace(namespace="default", resource_name="opa-65f7459b8f-ffzqg")
    assert is_control_plane_resource(ev_opa) is True

    ev_grafana = SimpleNamespace(
        namespace="monitoring", resource_name="grafana-agent-xyz"
    )
    assert is_control_plane_resource(ev_grafana) is True

    # User workload services
    ev_demo = SimpleNamespace(namespace="default", resource_name="shop-demo")
    assert is_control_plane_resource(ev_demo) is False

    ev_prom = SimpleNamespace(
        namespace="monitoring", resource_name="prometheus-grafana"
    )
    assert is_control_plane_resource(ev_prom) is False

    ev_checkout = SimpleNamespace(
        namespace="production", resource_name="checkout-service"
    )
    assert is_control_plane_resource(ev_checkout) is False


@pytest.mark.asyncio
async def test_workflow_events_skips_control_plane():
    """Verify handle_incoming_event silently skips control plane resources."""
    mock_event = SimpleNamespace(
        namespace="nexus",
        resource_name="nexus-api-7b8c",
        agent="k8s",
        timestamp=None,
    )

    mock_workflow = MagicMock()
    mock_workflow._correlator = MagicMock()
    mock_workflow._correlator.ingest = MagicMock()

    await handle_incoming_event(mock_workflow, mock_event)
    assert mock_workflow._correlator.ingest.call_count == 0
