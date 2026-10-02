"""
Kubernetes Read-Only Observation Tools
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexus.tools.base import NexusTool, NexusToolResult, ToolDomain, ToolRiskLevel
from nexus.tools.k8s.schemas import (
    DescribeResourceSchema,
    GetMetricsSchema,
    GetPodLogsSchema,
)


class K8sGetPodLogsTool(NexusTool):
    name = "k8s_get_pod_logs"
    description = "Fetch recent log lines from a Kubernetes pod to diagnose errors."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = GetPodLogsSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.get_pod_logs,
            namespace=args.namespace,
            pod_name=args.pod_name,
            tail_lines=args.tail_lines,
        )
        is_err = "Error fetching logs" in res
        return NexusToolResult(
            success=not is_err,
            data={"logs": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sDescribeResourceTool(NexusTool):
    name = "k8s_describe_resource"
    description = "Get detailed status and state of a Kubernetes pod, deployment, or service."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = DescribeResourceSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.describe_resource,
            namespace=args.namespace,
            resource_kind=args.resource_kind,
            resource_name=args.resource_name,
        )
        is_err = res.startswith("Error describing")
        return NexusToolResult(
            success=not is_err,
            data={"status": res} if not is_err else None,
            error=res if is_err else None,
        )


class K8sGetMetricsTool(NexusTool):
    name = "k8s_get_metrics"
    description = "Fetch CPU and Memory metrics for a pod from Prometheus."
    domain = ToolDomain.K8S
    risk_level = ToolRiskLevel.L0_OBSERVE
    args_schema = GetMetricsSchema

    async def execute(self, **kwargs: Any) -> NexusToolResult:
        from nexus.agents import k8s_tools
        args = self.args_schema(**kwargs)
        res = await asyncio.to_thread(
            k8s_tools.get_metrics,
            namespace=args.namespace,
            pod_name=args.pod_name,
        )
        is_err = "error fetching" in res
        return NexusToolResult(
            success=not is_err,
            data={"metrics": res} if not is_err else None,
            error=res if is_err else None,
        )
