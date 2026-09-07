"""
Unit tests for NEXUS multi-platform adapters (Kubernetes and AWS).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.graph.platform import (
    AWSPlatformAdapter,
    HealthCheckResult,
    K8sPlatformAdapter,
    PlatformRegistry,
    PlatformTelemetry,
    TargetResource,
)
from nexus.tools.base import NexusToolResult

# ── Kubernetes Platform Adapter Tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_k8s_adapter_target_detection():
    adapter = K8sPlatformAdapter()

    # 1. Standard pod crash event
    events = [
        {
            "agent": "k8s",
            "resource_name": "checkout-service-7bbd8f4c-x98qz",
            "namespace": "prod",
            "severity": "critical",
            "signal_type": "pod_crashloopbackoff",
        }
    ]
    target = adapter.detect_target(events)
    assert target.platform == "kubernetes"
    assert target.namespace == "prod"
    assert target.name == "checkout-service-7bbd8f4c-x98qz"
    assert target.kind == "pod"


@pytest.mark.asyncio
async def test_k8s_adapter_telemetry_collection_deterministic():
    """Verify that telemetry is gathered deterministically without a ReAct loop."""
    adapter = K8sPlatformAdapter()
    target = TargetResource(platform="kubernetes", namespace="default", name="payment-api", kind="deployment")

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="State: CrashLoopBackOff\nReplicas: 3"):
        with patch("nexus.agents.k8s_tools.get_pod_logs", return_value="panic: fatal memory error\nstack trace line 1"):
            telemetry: PlatformTelemetry = await adapter.collect_telemetry(target)

            assert telemetry.target.name == "payment-api"
            assert telemetry.health_status == "crashloopbackoff"
            assert len(telemetry.recent_logs) > 0
            assert "panic: fatal memory error" in telemetry.recent_logs[0]
            assert "CrashLoopBackOff" in telemetry.live_config.get("describe", "")


@pytest.mark.asyncio
async def test_k8s_adapter_snapshot_and_rollback():
    adapter = K8sPlatformAdapter()
    target = TargetResource(platform="kubernetes", namespace="default", name="payment-api", kind="deployment")

    # Capture scale snapshot
    with patch("nexus.agents.k8s_tools.describe_resource", return_value="Replicas: 4\nAvailable: 4"):
        snapshot = await adapter.capture_snapshot(
            target, "k8s_scale_resource", {"resource_name": "payment-api", "replicas": 8}
        )

        assert snapshot is not None
        assert snapshot.rollback_tool == "k8s_scale_resource"
        assert snapshot.rollback_parameters["replicas"] == 4

        # Execute rollback
        with patch.object(adapter, "execute_action", return_value=NexusToolResult(success=True)) as mock_exec:
            res = await adapter.execute_rollback(snapshot)
            assert res.success is True
            mock_exec.assert_called_once_with("k8s_scale_resource", snapshot.rollback_parameters)


@pytest.mark.asyncio
async def test_k8s_adapter_health_verification():
    adapter = K8sPlatformAdapter()
    target = TargetResource(platform="kubernetes", namespace="default", name="payment-api", kind="deployment")

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="All pods Running and Ready"):
        verif: HealthCheckResult = await adapter.verify_health(target, {})
        assert verif.healthy is True
        assert verif.slo_restored is True

    with patch("nexus.agents.k8s_tools.describe_resource", return_value="Pod in CrashLoopBackOff state"):
        verif2: HealthCheckResult = await adapter.verify_health(target, {})
        assert verif2.healthy is False
        assert verif2.slo_restored is False


# ── AWS Platform Adapter Tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aws_adapter_target_detection():
    adapter = AWSPlatformAdapter()

    # 1. From ARN
    arn = "arn:aws:lambda:us-east-1:123456789012:function:orders-processor"
    events = [
        {
            "agent": "cloudwatch",
            "resource_name": arn,
            "namespace": "us-east-1",
            "severity": "critical",
            "signal_type": "lambda_error_rate_high",
        }
    ]
    target = adapter.detect_target(events)
    assert target.platform == "aws"
    assert target.name == "orders-processor"
    assert target.kind == "lambda"
    assert target.namespace == "us-east-1"


@pytest.mark.asyncio
async def test_aws_adapter_telemetry_collection():
    adapter = AWSPlatformAdapter()
    target = TargetResource(platform="aws", namespace="us-east-1", name="orders-processor", kind="lambda")

    mock_cw_tool = MagicMock()
    mock_cw_tool.execute = AsyncMock(return_value=NexusToolResult(success=True, data={"Datapoints": [{"Sum": 42}]}))
    mock_log_tool = MagicMock()
    mock_log_tool.execute = AsyncMock(
        return_value=NexusToolResult(
            success=True,
            data={"events": [{"message": "Runtime.Exit: OutOfMemoryError, Memory Size: 128 MB"}]},
        )
    )

    adapter._tools["aws_get_metric_data"] = mock_cw_tool
    adapter._tools["aws_get_log_events"] = mock_log_tool

    with patch("nexus.graph.platform.aws._get_boto3_client") as mock_boto:
        client = MagicMock()
        client.get_function_configuration.return_value = {"MemorySize": 128, "Timeout": 15}
        mock_boto.return_value = client

        telemetry: PlatformTelemetry = await adapter.collect_telemetry(target)
        assert telemetry.target.name == "orders-processor"
        assert telemetry.health_status == "oomkilled"
        assert len(telemetry.recent_logs) == 1
        assert "OutOfMemoryError" in telemetry.recent_logs[0]
        assert telemetry.live_config.get("MemorySize") == 128


@pytest.mark.asyncio
async def test_aws_adapter_snapshot_and_rollback():
    adapter = AWSPlatformAdapter()
    target = TargetResource(platform="aws", namespace="us-east-1", name="orders-processor", kind="lambda")

    with patch("nexus.graph.platform.aws._get_boto3_client") as mock_boto:
        client = MagicMock()
        client.get_function_configuration.return_value = {"MemorySize": 128}
        mock_boto.return_value = client

        snapshot = await adapter.capture_snapshot(
            target, "aws_update_lambda_memory", {"function_name": "orders-processor", "memory_mb": 512}
        )

        assert snapshot is not None
        assert snapshot.rollback_tool == "aws_update_lambda_memory"
        assert snapshot.rollback_parameters["memory_mb"] == 128

        with patch.object(adapter, "execute_action", return_value=NexusToolResult(success=True)) as mock_exec:
            res = await adapter.execute_rollback(snapshot)
            assert res.success is True
            mock_exec.assert_called_once_with("aws_update_lambda_memory", snapshot.rollback_parameters)


# ── Platform Registry Dynamic Resolution Tests ────────────────────────────────

def test_platform_registry_resolution():
    registry = PlatformRegistry()
    k8s = K8sPlatformAdapter()
    aws = AWSPlatformAdapter()
    registry.register(k8s)
    registry.register(aws)

    # Resolve by explicit platform
    assert registry.resolve_adapter({"platform": "aws"}).platform_id == "aws"
    assert registry.resolve_adapter({"platform": "kubernetes"}).platform_id == "kubernetes"

    # Resolve by AWS agent
    assert registry.resolve_adapter({"agent": "lambda"}).platform_id == "aws"
    assert registry.resolve_adapter({"agent": "cloudwatch"}).platform_id == "aws"

    # Resolve by ARN
    assert registry.resolve_adapter({"resource_name": "arn:aws:sqs:us-east-1:123456:my-queue"}).platform_id == "aws"

    # Resolve by K8s agent / name
    assert registry.resolve_adapter({"agent": "k8s", "resource_name": "payment-api"}).platform_id == "kubernetes"
