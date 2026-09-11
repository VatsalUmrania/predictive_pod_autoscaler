"""
NEXUS Unified Tool Registry
===========================
Central catalog for all diagnostic and remediation tools across Kubernetes and AWS.
Manages tool discovery, parameter schema validation, risk classification,
Gemini function declarations, and audited execution.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nexus.tools.aws_adapter import (
    AWSGetLogEventsTool,
    AWSGetMetricDataTool,
    AWSReplaySQSDLQTool,
    AWSRollbackLambdaAliasTool,
    AWSUpdateLambdaMemoryTool,
    AWSUpdateLambdaTimeoutTool,
)
from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s_adapter import (
    K8sDescribeResourceTool,
    K8sGetMetricsTool,
    K8sGetPodLogsTool,
    K8sPatchResourceLimitsTool,
    K8sRestartDeploymentTool,
    K8sRollbackDeploymentTool,
    K8sScaleResourceTool,
)

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Registry managing available tools and audited execution."""

    def __init__(self) -> None:
        self._tools: dict[str, NexusTool] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        # K8s tools
        self.register(K8sGetPodLogsTool())
        self.register(K8sDescribeResourceTool())
        self.register(K8sGetMetricsTool())
        self.register(K8sRestartDeploymentTool())
        self.register(K8sScaleResourceTool())
        self.register(K8sRollbackDeploymentTool())
        self.register(K8sPatchResourceLimitsTool())

        # AWS tools
        self.register(AWSGetMetricDataTool())
        self.register(AWSGetLogEventsTool())
        self.register(AWSUpdateLambdaMemoryTool())
        self.register(AWSUpdateLambdaTimeoutTool())
        self.register(AWSRollbackLambdaAliasTool())
        self.register(AWSReplaySQSDLQTool())

    def register(self, tool: NexusTool) -> None:
        self._tools[tool.name] = tool
        logger.debug("[ToolRegistry] Registered tool: %s (%s, %s)", tool.name, tool.domain.value, tool.risk_level.value)

    def get(self, name: str) -> NexusTool | None:
        return self._tools.get(name)

    def list_tools(
        self,
        domain: ToolDomain | None = None,
        max_risk: ToolRiskLevel | None = None,
    ) -> list[NexusTool]:
        results = list(self._tools.values())
        if domain:
            results = [t for t in results if t.domain == domain]
        if max_risk:
            # Order: L0 < L1 < L2 < L3
            risk_order = {
                ToolRiskLevel.L0_OBSERVE: 0,
                ToolRiskLevel.L1_SAFE: 1,
                ToolRiskLevel.L2_MUTATE_APPROVAL: 2,
                ToolRiskLevel.L3_DESTRUCTIVE: 3,
            }
            max_level = risk_order.get(max_risk, 3)
            results = [t for t in results if risk_order.get(t.risk_level, 0) <= max_level]
        return results

    def get_function_declarations(
        self, domain: ToolDomain | None = None, max_risk: ToolRiskLevel | None = None
    ) -> list[dict[str, Any]]:
        """Return function calling declarations for LLMs."""
        tools = self.list_tools(domain=domain, max_risk=max_risk)
        return [t.to_function_declaration() for t in tools]

    async def execute_tool(
        self,
        name: str,
        parameters: dict[str, Any],
        run_id: str | None = None,
        db_client: Any = None,
    ) -> NexusToolResult:
        """
        Execute a tool by name with automatic database logging if db_client is provided.
        """
        tool = self.get(name)
        if not tool:
            return NexusToolResult(success=False, error=f"Tool '{name}' not found in registry")

        call_id = None
        start_time = time.monotonic()

        # Pre-log tool call in DB if client provided
        if db_client and run_id:
            try:
                category = f"{tool.domain.value[:3]}_{'mutate' if tool.risk_level != ToolRiskLevel.L0_OBSERVE else 'read'}"
                call_id = await db_client.log_tool_call(
                    run_id=run_id,
                    tool_name=name,
                    tool_category=category,
                    input_parameters=parameters,
                )
            except Exception as log_exc:
                logger.warning("[ToolRegistry] Failed to log tool call invocation: %s", log_exc)

        # Execute
        try:
            result = await tool.execute(**parameters)
        except Exception as exc:
            logger.error("[ToolRegistry] Error executing tool %s: %s", name, exc, exc_info=True)
            result = NexusToolResult(success=False, error=str(exc))

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Update tool call in DB
        if db_client and call_id:
            try:
                status = "success" if result.success else "failed"
                await db_client.update_tool_call(
                    call_id=call_id,
                    status=status,
                    output_result=result.data,
                    error_details=result.error,
                    duration_ms=duration_ms,
                )
            except Exception as log_exc:
                logger.warning("[ToolRegistry] Failed to update tool call outcome: %s", log_exc)

        return result


# Global singleton registry
default_registry = ToolRegistry()
