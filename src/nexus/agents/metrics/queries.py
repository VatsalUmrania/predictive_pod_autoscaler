"""
Prometheus Metrics Queries & Query Runner
"""

from __future__ import annotations

import logging
import math

import httpx

from nexus.agents.metrics.circuit_breaker import BOUNDS, PrometheusCircuitBreaker

logger = logging.getLogger(__name__)

DEFAULT_QUERIES: dict[str, str] = {
    "cpu_utilization_pct": (
        '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)'
    ),
    "memory_utilization_pct": (
        "100 * (1 - (avg(node_memory_MemAvailable_bytes) / avg(node_memory_MemTotal_bytes)))"
    ),
    "error_rate": (
        '(sum(rate(http_requests_total{status=~"5.."}[2m])) or vector(0))'
        " / (sum(rate(http_requests_total[2m])) > 0 or vector(1))"
    ),
    "rps": ("sum(rate(http_requests_total[1m])) or vector(0)"),
    "latency_p95_ms": (
        "(histogram_quantile(0.95,"
        "  sum(rate(http_request_duration_seconds_bucket[2m])) by (le)"
        ") or vector(0)) * 1000"
    ),
}


async def execute_promql_query(
    prom_url: str,
    name: str,
    promql: str,
    cb: PrometheusCircuitBreaker,
    timeout: float = 5.0,
) -> float | None:
    """Execute a single PromQL query with circuit breaker and NaN/Inf clamping."""
    if not cb.can_attempt():
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{prom_url}/api/v1/query",
                params={"query": promql},
            )
            resp.raise_for_status()
            results = resp.json().get("data", {}).get("result", [])
            if not results:
                return None

            value = float(results[0]["value"][1])

            if math.isnan(value) or math.isinf(value):
                logger.warning(f"[MetricsAgent] NaN/Inf in '{name}', skipping")
                return None

            if name in BOUNDS:
                lo, hi = BOUNDS[name]
                if value < lo or value > hi:
                    logger.debug(
                        f"[MetricsAgent] Clamping {name}={value:.2f} to [{lo}, {hi}]"
                    )
                    value = max(lo, min(hi, value))

            cb.record_success()
            return value

    except httpx.TimeoutException:
        cb.record_failure()
        logger.warning(f"[MetricsAgent] Prometheus timeout on '{name}'")
    except httpx.HTTPStatusError as exc:
        cb.record_failure()
        logger.warning(f"[MetricsAgent] HTTP {exc.response.status_code} on '{name}'")
    except Exception as exc:
        cb.record_failure()
        logger.error(f"[MetricsAgent] Query '{name}' failed: {exc}")

    return None
