# Schema Version: v3 — foundation model: scale-invariant normalized features
# Requires: CPU + memory limits set on target deployment
# Requires: Istio sidecar or /metrics HTTP endpoint
# Breaking change from v2: normalized RPS/latency replace raw values; target is multiplier
"""Shared feature ordering used across export, training, and online inference."""

QUERIED_FEATURES = [
    "normalized_rps",
    "cpu_utilization_pct",
    "memory_utilization_pct",
    "latency_normalized",
    "error_rate",
    "rps_acceleration",
    "cpu_acceleration",
]

TEMPORAL_FEATURES = [
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
]

FEATURE_COLUMNS = QUERIED_FEATURES + TEMPORAL_FEATURES
TARGET_COLUMNS = [
    "normalized_rps_t3m",
    "normalized_rps_t5m",
    "normalized_rps_t10m",
]

NUM_FEATURES = len(FEATURE_COLUMNS)
