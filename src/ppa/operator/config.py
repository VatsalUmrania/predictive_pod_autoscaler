"""DEPRECATED: Import from ppa.config instead.

    from ppa.config import (
        PROMETHEUS_URL, NAMESPACE, TIMER_INTERVAL, INITIAL_DELAY,
        STABILIZATION_STEPS, STABILIZATION_TOLERANCE, LOOKBACK_STEPS,
        PROM_FAILURE_THRESHOLD, get_prometheus_url,
        DEFAULT_CAPACITY_PER_POD, DEFAULT_MIN_REPLICAS, DEFAULT_MAX_REPLICAS,
        DEFAULT_SCALE_UP_RATE, DEFAULT_SCALE_DOWN_RATE, DEFAULT_MODEL_DIR,
        FeatureVectorException,
    )

This module exists for backward compatibility and will be removed in a future version.
"""

import warnings

warnings.warn(
    "ppa.operator.config is deprecated. Import from ppa.config instead.",
    DeprecationWarning,
    stacklevel=2,
)

from ppa.config import (
    FeatureVectorException,
    PROMETHEUS_URL,
    get_prometheus_url,
)

NAMESPACE = "default"
TIMER_INTERVAL = 30
INITIAL_DELAY = 60
STABILIZATION_STEPS = 2
STABILIZATION_TOLERANCE = 0.5
LOOKBACK_STEPS = 60
PROM_FAILURE_THRESHOLD = 10

DEFAULT_CAPACITY_PER_POD = 50
DEFAULT_MIN_REPLICAS = 2
DEFAULT_MAX_REPLICAS = 20
DEFAULT_SCALE_UP_RATE = 2.0
DEFAULT_SCALE_DOWN_RATE = 0.5
DEFAULT_MODEL_DIR = "/models"

__all__ = [
    "DEFAULT_CAPACITY_PER_POD",
    "DEFAULT_MAX_REPLICAS",
    "DEFAULT_MIN_REPLICAS",
    "DEFAULT_MODEL_DIR",
    "DEFAULT_SCALE_DOWN_RATE",
    "DEFAULT_SCALE_UP_RATE",
    "FeatureVectorException",
    "INITIAL_DELAY",
    "LOOKBACK_STEPS",
    "NAMESPACE",
    "PROM_FAILURE_THRESHOLD",
    "PROMETHEUS_URL",
    "STABILIZATION_STEPS",
    "STABILIZATION_TOLERANCE",
    "TIMER_INTERVAL",
    "get_prometheus_url",
]
