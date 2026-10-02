"""
NEXUS AWS Platform Adapter
==========================
Platform adapter implementation for AWS serverless & cloud infrastructure.
Integrates with boto3 and nexus.tools.aws_adapter (Lambda, SQS, CloudWatch, DynamoDB).
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.graph.platform.aws_snapshot import capture_aws_snapshot
from nexus.graph.platform.aws_target import can_handle_aws, detect_aws_target
from nexus.graph.platform.aws_telemetry import (
    collect_aws_telemetry,
    inspect_aws_live_state,
    verify_aws_health,
)
from nexus.graph.platform.base import (
    BasePlatformAdapter,
    HealthCheckResult,
    LiveStateSnapshot,
    PlatformTelemetry,
    RollbackSnapshot,
    TargetResource,
)
from nexus.tools.aws_adapter import (
    AWSGetLogEventsTool,
    AWSGetMetricDataTool,
    AWSReplaySQSDLQTool,
    AWSRollbackLambdaAliasTool,
    AWSUpdateLambdaMemoryTool,
    AWSUpdateLambdaTimeoutTool,
)
from nexus.tools.base import NexusTool, NexusToolResult

logger = logging.getLogger(__name__)


def _get_boto3_client(service: str, region_name: str | None = None) -> Any:
    """Helper to instantiate boto3 clients; mock target for tests."""
    import boto3

    return boto3.client(service, region_name=region_name)


class AWSPlatformAdapter(BasePlatformAdapter):
    """Platform adapter managing AWS observability, remediation, and rollbacks."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {
            AWSGetMetricDataTool.name: AWSGetMetricDataTool(),
            AWSGetLogEventsTool.name: AWSGetLogEventsTool(),
            AWSUpdateLambdaMemoryTool.name: AWSUpdateLambdaMemoryTool(),
            AWSUpdateLambdaTimeoutTool.name: AWSUpdateLambdaTimeoutTool(),
            AWSRollbackLambdaAliasTool.name: AWSRollbackLambdaAliasTool(),
            AWSReplaySQSDLQTool.name: AWSReplaySQSDLQTool(),
        }

    @property
    def platform_id(self) -> str:
        return "aws"

    def can_handle(self, event_or_target: dict[str, Any] | TargetResource) -> bool:
        return can_handle_aws(event_or_target)

    def detect_target(self, events: list[dict[str, Any]]) -> TargetResource:
        return detect_aws_target(events)

    async def collect_telemetry(self, target: TargetResource) -> PlatformTelemetry:
        return await collect_aws_telemetry(target, self._tools)

    async def inspect_live_state(self, target: TargetResource) -> LiveStateSnapshot:
        return await inspect_aws_live_state(target)

    def get_tools(self) -> list[NexusTool]:
        return list(self._tools.values())

    async def execute_action(
        self, tool_name: str, parameters: dict[str, Any]
    ) -> NexusToolResult:
        tool = self._tools.get(tool_name)
        if not tool:
            return NexusToolResult(
                success=False,
                error=f"Tool '{tool_name}' not registered in AWSPlatformAdapter",
            )
        return await tool.execute(**parameters)

    async def capture_snapshot(
        self, target: TargetResource, tool_name: str, parameters: dict[str, Any]
    ) -> RollbackSnapshot | None:
        return await capture_aws_snapshot(target, tool_name, parameters)

    async def execute_rollback(self, snapshot: RollbackSnapshot) -> NexusToolResult:
        logger.info("[AWSPlatformAdapter] Executing rollback snapshot %s", snapshot.snapshot_id)
        return await self.execute_action(
            snapshot.rollback_tool, snapshot.rollback_parameters
        )

    async def verify_health(
        self, target: TargetResource, plan_step: dict[str, Any]
    ) -> HealthCheckResult:
        return await verify_aws_health(target, plan_step)
