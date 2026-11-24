"""
Data structures and math helpers for PPA outcome tracking.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def compute_smape(predicted: float, actual: float) -> float:
    """Symmetric Mean Absolute Percentage Error — safe for zero values."""
    denominator = (abs(predicted) + abs(actual)) / 2.0
    if denominator == 0.0:
        return 0.0
    return 100.0 * abs(predicted - actual) / denominator


@dataclass
class PendingPrediction:
    decision_id: str
    deployment: str
    namespace: str
    predicted_rps: float
    current_rps: float
    confidence: float
    horizon_minutes: int
    model_version: str
    raw_features: dict[str, float]
    expected_resolution_time: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expected_resolution_time


def fetch_actual_rps(deployment: str, prom_url: str | None = None) -> float:
    """Fetch current RPS for a deployment from Prometheus."""
    url = prom_url or os.environ.get("PROMETHEUS_URL")
    if url is None:
        try:
            from ppa.config import get_prometheus_url

            url = get_prometheus_url()
        except Exception:
            url = "http://prometheus:9090"
    try:
        from prometheus_api_client import PrometheusConnect

        prom = PrometheusConnect(url=url)
        query = f'sum(rate(nginx_ingress_requests_total{{backend=~"{deployment}.*"}}[1m])) * 60'
        result = prom.custom_query(query=query)
        if result and result[0]["value"][1] != "NaN":
            return float(result[0]["value"][1])
    except Exception as exc:
        logger.warning(
            f"[PpaOutcomeTracker] Failed to fetch actual RPS for {deployment}: {exc}"
        )
    return 0.0
