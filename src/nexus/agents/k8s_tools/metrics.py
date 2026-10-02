"""
Prometheus Metrics Reader for Pods
"""

from __future__ import annotations

import logging

import httpx

from nexus.agents.k8s_tools.client import PROMETHEUS_URL

logger = logging.getLogger(__name__)


def get_metrics(namespace: str, pod_name: str) -> str:
    """Fetch real CPU/Memory metrics for a pod from Prometheus.

    Queries container_cpu_usage_seconds_total and container_memory_working_set_bytes
    via PromQL, returns human-readable string.
    """
    try:
        cpu_query = (
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
            f'pod="{pod_name}",container!=""}}[1m])) by (pod)'
        )
        mem_query = (
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
            f'pod="{pod_name}",container!=""}}) by (pod)'
        )

        cpu_val = None
        mem_val = None

        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": cpu_query},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                if data:
                    cpu_val = float(data[0]["value"][1])

            resp = client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": mem_query},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("result", [])
                if data:
                    mem_val = float(data[0]["value"][1])

        if cpu_val is not None and mem_val is not None:
            cpu_m = cpu_val * 1000
            mem_mi = mem_val / (1024 * 1024)
            return f"Metrics for {pod_name} in {namespace}: CPU {cpu_m:.0f}m, Memory {mem_mi:.0f}Mi"
        elif cpu_val is not None:
            cpu_m = cpu_val * 1000
            return f"Metrics for {pod_name} in {namespace}: CPU {cpu_m:.0f}m, Memory unavailable"
        elif mem_val is not None:
            mem_mi = mem_val / (1024 * 1024)
            return f"Metrics for {pod_name} in {namespace}: CPU unavailable, Memory {mem_mi:.0f}Mi"
        else:
            return f"Metrics for {pod_name} in {namespace}: no data from Prometheus"

    except Exception as e:
        logger.warning(f"get_metrics failed for {pod_name}: {e}")
        return f"Metrics for {pod_name} in {namespace}: error fetching ({e})"
