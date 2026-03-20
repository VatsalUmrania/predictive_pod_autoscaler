"""Predictive Pod Autoscaler (PPA) - Intelligent Kubernetes Scaling."""

__version__ = "1.0.0"

from ppa.common.constants import CAPACITY_PER_POD
from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES, TARGET_COLUMNS

__all__ = [
    "__version__",
    "CAPACITY_PER_POD",
    "FEATURE_COLUMNS",
    "NUM_FEATURES",
    "TARGET_COLUMNS",
]
