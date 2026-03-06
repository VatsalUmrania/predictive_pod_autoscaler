"""Unit tests for the shared feature contract."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.constants import CAPACITY_PER_POD, GAP_THRESHOLD_MINUTES
from common.feature_spec import (
    FEATURE_COLUMNS,
    NUM_FEATURES,
    QUERIED_FEATURES,
    TARGET_COLUMNS,
    TEMPORAL_FEATURES,
)
from common.promql import LATENCY_WINDOW, RATE_WINDOW, build_queries


class TestFeatureSpec:
    def test_feature_count(self):
        assert len(FEATURE_COLUMNS) == 14

    def test_num_features_matches(self):
        assert NUM_FEATURES == len(FEATURE_COLUMNS)

    def test_no_duplicate_features(self):
        assert len(set(FEATURE_COLUMNS)) == len(FEATURE_COLUMNS)

    def test_no_duplicate_targets(self):
        assert len(set(TARGET_COLUMNS)) == len(TARGET_COLUMNS)

    def test_temporal_features_are_appended(self):
        assert FEATURE_COLUMNS[-len(TEMPORAL_FEATURES):] == TEMPORAL_FEATURES
        assert set(FEATURE_COLUMNS) == set(QUERIED_FEATURES) | set(TEMPORAL_FEATURES)


class TestPromQL:
    def test_build_queries_returns_all_queried_features(self):
        queries = build_queries("test-app", "default", "test-app")
        assert set(queries.keys()) == set(QUERIED_FEATURES)

    def test_queries_are_namespace_scoped(self):
        queries = build_queries("my-app", "production", "my-app")
        for name, query in queries.items():
            assert 'namespace="production"' in query, f"Query {name} is not namespace-scoped"

    def test_cpu_uses_avg(self):
        query = build_queries("test-app", "default", "test-app")["cpu_usage_percent"]
        assert query.startswith("avg(")

    def test_memory_uses_avg(self):
        query = build_queries("test-app", "default", "test-app")["memory_usage_bytes"]
        assert query.startswith("avg(")

    def test_rps_uses_sum(self):
        query = build_queries("test-app", "default", "test-app")["requests_per_second"]
        assert query.startswith("sum(")

    def test_rate_window_is_consistent(self):
        queries = build_queries("test-app", "default", "test-app")
        assert f"[{RATE_WINDOW}]" in queries["requests_per_second"]
        assert f"[{RATE_WINDOW}]" in queries["error_rate"]

    def test_latency_uses_wider_window(self):
        query = build_queries("test-app", "default", "test-app")["latency_p95_ms"]
        assert f"[{LATENCY_WINDOW}]" in query


class TestConstants:
    def test_capacity_per_pod_positive(self):
        assert CAPACITY_PER_POD > 0

    def test_gap_threshold_positive(self):
        assert GAP_THRESHOLD_MINUTES > 0
