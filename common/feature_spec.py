"""Shared feature ordering used across export, training, and online inference."""

QUERIED_FEATURES = [
    "requests_per_second",
    "cpu_usage_percent",
    "memory_usage_bytes",
    "latency_p95_ms",
    "active_connections",
    "error_rate",
    "cpu_acceleration",
    "rps_acceleration",
    "current_replicas",
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
    "rps_t5",
    "rps_t10",
    "rps_t15",
    "replicas_t5",
    "replicas_t10",
    "replicas_t15",
]

NUM_FEATURES = len(FEATURE_COLUMNS)
