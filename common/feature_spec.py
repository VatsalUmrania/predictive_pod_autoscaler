# Schema Version: v2 — normalized universal features
# Requires: CPU + memory limits set on target deployment
# Requires: Istio sidecar or /metrics HTTP endpoint
# Breaking change from v1: do not mix v1 and v2 CSVs
"""Shared feature ordering used across export, training, and online inference."""

QUERIED_FEATURES = [
    "rps_per_replica",
    "cpu_utilization_pct",
    "memory_utilization_pct",
    "latency_p95_ms",
    "active_connections",
    "error_rate",
    "cpu_acceleration",
    "rps_acceleration",
    "replicas_normalized",
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
    "rps_t3m",
    "rps_t5m",
    "rps_t10m",
    "replicas_t3m",
    "replicas_t5m",
    "replicas_t10m",
]

NUM_FEATURES = len(FEATURE_COLUMNS)
