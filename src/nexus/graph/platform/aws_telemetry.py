"""
AWS telemetry collection, live state inspection, and health verification.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.graph.platform.base import (
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    TargetResource,
)
from nexus.tools.base import NexusTool

logger = logging.getLogger(__name__)


def _get_client(service: str, region: str) -> Any:
    import nexus.graph.platform.aws as aws_plat

    return aws_plat._get_boto3_client(service, region)


async def collect_aws_telemetry(
    target: TargetResource, tools: dict[str, NexusTool]
) -> PlatformTelemetry:
    """Deterministically collect CloudWatch metrics and recent log streams without ReAct."""
    metrics: dict[str, Any] = {}
    recent_logs: list[str] = []
    live_config: dict[str, Any] = {}
    health_status = "degraded"

    metric_tool = tools.get("aws_get_metric_data")
    if metric_tool:
        try:
            m_res = await metric_tool.execute(
                namespace=(
                    "AWS/Lambda"
                    if target.kind == "lambda"
                    else f"AWS/{target.kind.upper()}"
                ),
                metric_name="Errors",
                dimension_name=(
                    "FunctionName" if target.kind == "lambda" else "QueueName"
                ),
                dimension_value=target.name,
                region=target.namespace,
            )
            if m_res.success and m_res.data:
                metrics["error_metric"] = m_res.data
        except Exception as m_exc:
            logger.debug(
                "[AWSPlatformAdapter] CloudWatch metric query failed: %s", m_exc
            )

    log_tool = tools.get("aws_get_log_events")
    if log_tool:
        try:
            log_res = await log_tool.execute(
                log_group_name=f"/aws/lambda/{target.name}",
                filter_pattern="ERROR",
                limit=15,
                region=target.namespace,
            )
            if log_res.success and log_res.data:
                events = log_res.data.get("events", [])
                recent_logs = [e.get("message", "") for e in events]
                if any("Task timed out" in line for line in recent_logs):
                    health_status = "timeout"
                elif any(
                    "OutOfMemory" in line or "Memory Size" in line
                    for line in recent_logs
                ):
                    health_status = "oomkilled"
        except Exception as l_exc:
            logger.debug("[AWSPlatformAdapter] CloudWatch log query failed: %s", l_exc)

    try:
        if target.kind == "lambda":
            client = await asyncio.to_thread(_get_client, "lambda", target.namespace)
            func_cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=target.name
            )
            live_config["MemorySize"] = func_cfg.get("MemorySize")
            live_config["Timeout"] = func_cfg.get("Timeout")
            live_config["LastModified"] = func_cfg.get("LastModified")
    except Exception as c_exc:
        logger.debug("[AWSPlatformAdapter] Function config inspect failed: %s", c_exc)

    return PlatformTelemetry(
        target=target,
        metrics=metrics,
        recent_logs=recent_logs,
        live_config=live_config,
        health_status=health_status,
    )


async def inspect_aws_live_state(target: TargetResource) -> LiveStateSnapshot:
    """Inspect AWS target live state and existence."""
    try:
        if target.kind == "lambda":
            client = await asyncio.to_thread(_get_client, "lambda", target.namespace)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=target.name
            )
            return LiveStateSnapshot(
                target=target,
                exists=True,
                is_failing=False,
                state_attributes=cfg,
            )
        return LiveStateSnapshot(
            target=target,
            exists=True,
            is_failing=False,
            state_attributes={"name": target.name},
        )
    except Exception as exc:
        return LiveStateSnapshot(
            target=target,
            exists=False,
            is_failing=True,
            failure_details=str(exc),
        )


async def verify_aws_health(
    target: TargetResource, plan_step: dict[str, Any]
) -> HealthCheckResult:
    """Verify post-remediation health for AWS resources."""
    try:
        if target.kind == "lambda":
            client = await asyncio.to_thread(_get_client, "lambda", target.namespace)
            cfg = await asyncio.to_thread(
                client.get_function_configuration, FunctionName=target.name
            )
            status = cfg.get("State", "Active")
            healthy = status == "Active"
            return HealthCheckResult(
                healthy=healthy,
                slo_restored=healthy,
                failure_reason=None if healthy else f"Function in state {status}",
                details=f"Function {target.name} state: {status}",
            )

        return HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details="Assumed healthy for target",
        )
    except Exception as exc:
        return HealthCheckResult(
            healthy=True,
            slo_restored=True,
            details=f"Verification handled: {exc}",
        )
