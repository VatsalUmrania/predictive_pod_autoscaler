"""Test fixtures for mocking Prometheus responses."""

import pytest


class MockPrometheusResult:
    """Mock Prometheus query result."""

    def __init__(self, value):
        self.value = value


class MockPrometheusData:
    """Mock Prometheus data container."""

    def __init__(self, results):
        self.result = results


class MockPrometheusResponse:
    """Mock Prometheus API response."""

    def __init__(self, results, status="success"):
        self.json_data = {
            "status": status,
            "data": {
                "resultType": "vector",
                "result": results,
            },
        }

    def json(self):
        return self.json_data

    def raise_for_status(self):
        pass


@pytest.fixture
def prom_query_result():
    """Create a mock Prometheus query result with a single value."""

    def _create(value: float, timestamp: float = 1234567890.0):
        return MockPrometheusResponse(
            [
                {
                    "metric": {"pod": "test-app-abc123"},
                    "value": [timestamp, str(value)],
                }
            ]
        )

    return _create


@pytest.fixture
def prom_range_result():
    """Create a mock Prometheus range query result."""

    def _create(values: list[tuple[float, float]]):
        return MockPrometheusResponse(
            [
                {
                    "metric": {"pod": "test-app-abc123"},
                    "values": values,
                }
            ]
        )

    return _create
