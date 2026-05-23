"""Unit tests for feature specification and PromQL builders."""

from pathlib import Path

import yaml

from ppa.common.feature_spec import (
    FEATURE_COLUMNS,
    NUM_FEATURES,
    QUERIED_FEATURES,
    TARGET_COLUMNS,
    TEMPORAL_FEATURES,
)
from ppa.common.promql import (
    LATENCY_WINDOW,
    RATE_WINDOW,
    build_queries,
)


class TestFeatureSpec:
    def test_feature_count(self):
        assert NUM_FEATURES == 14

    def test_num_features_matches(self):
        assert len(FEATURE_COLUMNS) == NUM_FEATURES

    def test_no_duplicate_features(self):
        assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS))

    def test_no_duplicate_targets(self):
        assert len(TARGET_COLUMNS) == len(set(TARGET_COLUMNS))

    def test_temporal_features_are_appended(self):
        assert FEATURE_COLUMNS[: len(QUERIED_FEATURES)] == QUERIED_FEATURES
        assert FEATURE_COLUMNS[len(QUERIED_FEATURES) :] == TEMPORAL_FEATURES


class TestPromQL:
    def test_build_queries_returns_expected_keys(self):
        queries = build_queries("test-app", "default")
        expected_keys = [
            "requests_per_second",
            "cpu_utilization_pct",
            "memory_utilization_pct",
            "latency_p95_ms",
            "active_connections",
            "error_rate",
            "cpu_acceleration",
            "rps_acceleration",
            "current_replicas",
        ]
        for key in expected_keys:
            assert key in queries

    def test_queries_are_namespace_scoped(self):
        queries = build_queries("test-app", "test-ns")
        for query in queries.values():
            assert 'namespace="test-ns"' in query

    def test_cpu_uses_avg(self):
        queries = build_queries("test-app", "default")
        assert "rate(" in queries["cpu_utilization_pct"]

    def test_memory_uses_avg(self):
        queries = build_queries("test-app", "default")
        assert "working_set_bytes" in queries["memory_utilization_pct"]

    def test_resource_queries_fallback_to_pod_level_cadvisor_metrics(self):
        queries = build_queries("test-app", "default")

        cpu_query = queries["cpu_utilization_pct"]
        memory_query = queries["memory_utilization_pct"]

        assert 'container!="POD"' in cpu_query
        assert 'container!=""' not in cpu_query
        assert 'container!="POD"' in memory_query
        assert 'container!=""' not in memory_query

    def test_rps_uses_sum(self):
        queries = build_queries("test-app", "default")
        assert "sum(rate(" in queries["requests_per_second"]

    def test_rps_defaults_to_zero_when_counter_series_are_absent(self):
        query = build_queries("test-app", "default")["requests_per_second"]
        assert "vector(0)" in query
        assert "or on()" in query

    def test_rate_window_is_used(self):
        queries = build_queries("test-app", "default")
        for key in ["requests_per_second", "error_rate"]:
            assert f"[{RATE_WINDOW}]" in queries[key]

    def test_latency_uses_wider_window(self):
        queries = build_queries("test-app", "default")
        assert f"[{LATENCY_WINDOW}]" in queries["latency_p95_ms"]


class TestConstants:
    def test_capacity_per_pod_positive(self):
        from ppa.common.constants import CAPACITY_PER_POD

        assert CAPACITY_PER_POD > 0

    def test_gap_threshold_positive(self):
        from ppa.common.constants import GAP_THRESHOLD_MINUTES

        assert GAP_THRESHOLD_MINUTES > 0


class TestTestAppManifest:
    MANIFEST_PATH = Path(__file__).resolve().parents[2] / "data" / "test-app" / "deployment.yaml"

    def _load_docs(self):
        with self.MANIFEST_PATH.open() as handle:
            return [doc for doc in yaml.safe_load_all(handle) if doc]

    def test_deployment_and_service_share_namespace(self):
        docs = self._load_docs()
        resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

        deployment = resources[("Deployment", "test-app")]
        service = resources[("Service", "test-app")]

        assert deployment["metadata"]["namespace"] == "default"
        assert service["metadata"]["namespace"] == deployment["metadata"]["namespace"]

    def test_podmonitor_scrapes_the_workload_namespace(self):
        docs = self._load_docs()
        resources = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}

        deployment = resources[("Deployment", "test-app")]
        podmonitor = resources[("PodMonitor", "test-app")]

        assert podmonitor["metadata"]["namespace"] == "monitoring"
        assert podmonitor["spec"]["namespaceSelector"]["matchNames"] == [
            deployment["metadata"]["namespace"]
        ]
