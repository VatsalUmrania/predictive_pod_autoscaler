"""Predictive Pod Autoscaler (PPA) - Intelligent Kubernetes Scaling."""

__version__ = "1.0.0"

# Optional imports for modules that may not be available in all environments
# (e.g., operator container excludes training/model modules)
try:
    from ppa.common.constants import CAPACITY_PER_POD
    from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES, TARGET_COLUMNS
except ImportError:
    # If common module is not available, set defaults for operator-only container
    CAPACITY_PER_POD = 80
    FEATURE_COLUMNS = []
    NUM_FEATURES = 0
    TARGET_COLUMNS = []

__all__ = [
    "__version__",
    "CAPACITY_PER_POD",
    "FEATURE_COLUMNS",
    "NUM_FEATURES",
    "TARGET_COLUMNS",
]
