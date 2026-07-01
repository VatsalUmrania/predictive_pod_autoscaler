"""
Phase 4 — Collector: Service Dependencies
==========================================
Discovers what downstream services a Lambda function calls.
Answers: "Is the problem in this Lambda, or in a service it depends on?"

Uses AWS X-Ray service map and Lambda configuration to infer dependencies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Dependency:
    """A downstream service that a Lambda depends on."""
    name: str
    service_type: str    # "AWS::DynamoDB", "AWS::SQS", "HTTP", etc.
    is_healthy: bool
    avg_latency_ms: float
    fault_rate: float    # 0.0–1.0
    error_rate: float    # 0.0–1.0


@dataclass
class DependencyMap:
    """All dependencies of a Lambda function."""
    function_name: str
    dependencies: list[Dependency] = field(default_factory=list)
    unhealthy_deps: list[str] = field(default_factory=list)

    @property
    def has_unhealthy_deps(self) -> bool:
        return len(self.unhealthy_deps) > 0


def discover_dependencies(
    xray_client,
    function_name: str,
    window_minutes: int = 10,
) -> DependencyMap:
    """
    Use X-Ray service map to discover what services this function calls.
    Returns DependencyMap with health status for each dependency.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)
    deps: list[Dependency] = []

    try:
        resp = xray_client.get_service_graph(StartTime=start, EndTime=end)
        services = resp.get("Services", [])

        # Find the Lambda node
        lambda_node = next(
            (s for s in services if function_name in s.get("Name", "")), None
        )
        if not lambda_node:
            return DependencyMap(function_name=function_name)

        # Find downstream services (edges from Lambda)
        lambda_ref_id = lambda_node.get("ReferenceId", -1)
        for edge in lambda_node.get("Edges", []):
            target_id = edge.get("ReferenceId", -1)
            # Find the target service
            target = next((s for s in services if s.get("ReferenceId") == target_id), None)
            if not target:
                continue

            summary = edge.get("SummaryStatistics", {})
            total = summary.get("TotalCount", 1) or 1
            fault_rate = summary.get("FaultStatistics", {}).get("TotalCount", 0) / total
            error_rate = summary.get("ErrorStatistics", {}).get("TotalCount", 0) / total
            avg_latency = summary.get("ResponseTimeHistogram", [{}])
            avg_lat_ms = float(avg_latency[0].get("Value", 0)) * 1000 if avg_latency else 0

            dep = Dependency(
                name=target.get("Name", "unknown"),
                service_type=target.get("Type", "unknown"),
                is_healthy=(fault_rate < 0.05 and error_rate < 0.1),
                avg_latency_ms=avg_lat_ms,
                fault_rate=fault_rate,
                error_rate=error_rate,
            )
            deps.append(dep)

    except Exception as exc:
        logger.debug(f"[Dependencies] discover({function_name}): {exc}")

    unhealthy = [d.name for d in deps if not d.is_healthy]
    return DependencyMap(
        function_name=function_name,
        dependencies=deps,
        unhealthy_deps=unhealthy,
    )


def to_context_dict(dep_map: DependencyMap) -> dict[str, Any]:
    """Convert DependencyMap to a plain dict for the LLM prompt."""
    return {
        "dependency_count": len(dep_map.dependencies),
        "has_unhealthy_deps": dep_map.has_unhealthy_deps,
        "unhealthy_deps": dep_map.unhealthy_deps,
        "dependencies": [
            {
                "name": d.name,
                "type": d.service_type,
                "healthy": d.is_healthy,
                "fault_rate": f"{d.fault_rate:.1%}",
                "avg_latency_ms": round(d.avg_latency_ms, 1),
            }
            for d in dep_map.dependencies
        ],
    }
