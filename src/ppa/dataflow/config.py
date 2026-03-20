import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ppa.common.feature_spec import FEATURE_COLUMNS, QUERIED_FEATURES, TARGET_COLUMNS
from ppa.common.promql import (
    BASELINE_WINDOW,
    LATENCY_WINDOW,
    RATE_WINDOW,
    build_queries,
)

TARGET_APP = os.getenv("TARGET_APP", "test-app")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
NAMESPACE = os.getenv("NAMESPACE", "default")
CONTAINER_NAME = os.getenv("CONTAINER_NAME", "test-app")

QUERIES = build_queries(TARGET_APP, NAMESPACE, CONTAINER_NAME)
REQUIRED_QUERY_FEATURES = list(QUERIED_FEATURES)

__all__ = [
    "BASELINE_WINDOW",
    "CONTAINER_NAME",
    "FEATURE_COLUMNS",
    "LATENCY_WINDOW",
    "NAMESPACE",
    "PROMETHEUS_URL",
    "QUERIES",
    "RATE_WINDOW",
    "REQUIRED_QUERY_FEATURES",
    "TARGET_APP",
    "TARGET_COLUMNS",
]
