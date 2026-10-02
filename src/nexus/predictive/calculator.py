"""Replica calculations and horizon parsing for prescaling."""
from __future__ import annotations

import re


def parse_horizon(raw: object) -> int:
    """Parse PPA horizon field: 'rps_t10m' -> 10, 'rps_t5m' -> 5, or int passthrough."""
    if isinstance(raw, int):
        return raw
    s = str(raw)
    m = re.search(r"(\d+)m$", s)
    if m:
        return int(m.group(1))
    try:
        return int(s)
    except (ValueError, TypeError):
        return 10


def endpoint_to_deployment(endpoint: str) -> str:
    """Heuristic: /api/payments -> payments-api."""
    parts = [p for p in endpoint.strip("/").split("/") if p]
    if parts:
        name = parts[-1].replace("_", "-")
        return f"{name}-api" if not name.endswith("-api") else name
    return "unknown"


def compute_recommended_replicas(
    predicted_rps: float,
    target_rps: float,
    max_replicas: int,
) -> int:
    """Calculate target replicas clamped to [1, max_replicas]."""
    raw = int(predicted_rps / max(target_rps, 1.0))
    return min(max_replicas, max(1, raw))
