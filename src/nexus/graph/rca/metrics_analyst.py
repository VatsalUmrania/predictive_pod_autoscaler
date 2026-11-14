"""
Metrics Analyst Agent for saturation, error rates, and resource metrics.
"""

from __future__ import annotations

from typing import Any


class MetricsAnalystAgent:
    """Specialist agent analyzing saturation, error rates, latency, and resource metrics."""

    def analyze(
        self, metrics: dict[str, Any], raw_signals: list[dict[str, Any]]
    ) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        confidence_delta = 0.0

        error_rate = float(metrics.get("error_rate", 0.0))
        p99_latency_ms = float(
            metrics.get("p99_latency_ms", metrics.get("latency_p99", 0.0))
        )
        memory_usage_pct = float(
            metrics.get("memory_usage_pct", metrics.get("memory_usage", 0.0))
        )
        cpu_usage_pct = float(
            metrics.get("cpu_usage_pct", metrics.get("cpu_usage", 0.0))
        )
        restarts = int(metrics.get("restart_count", 0))

        if error_rate >= 0.05 or any(
            s.get("signal_type") == "high_error_rate" for s in raw_signals
        ):
            findings.append(f"Elevated HTTP/gRPC error rate: {error_rate:.1%}")
            signatures.append("elevated_error_rate")
            confidence_delta += 0.20

        if p99_latency_ms > 1000.0 or any(
            s.get("signal_type") == "high_latency" for s in raw_signals
        ):
            findings.append(f"Severe latency degradation (p99={p99_latency_ms:.0f}ms)")
            signatures.append("latency_spike")
            confidence_delta += 0.15

        if memory_usage_pct > 85.0:
            findings.append(f"Memory saturation critical: {memory_usage_pct:.1f}%")
            signatures.append("memory_saturation")
            confidence_delta += 0.20

        if restarts > 0:
            findings.append(f"Pod restart count actively incrementing: {restarts}")
            signatures.append("pod_restarts")
            confidence_delta += 0.20

        return {
            "agent": "MetricsAnalyst",
            "findings": findings,
            "signatures": signatures,
            "confidence_delta": confidence_delta,
            "metrics_summary": {
                "error_rate": error_rate,
                "p99_latency_ms": p99_latency_ms,
                "memory_pct": memory_usage_pct,
                "cpu_pct": cpu_usage_pct,
                "restarts": restarts,
            },
        }
