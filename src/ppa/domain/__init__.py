"""Pure domain logic for PPA - independent of Kubernetes, Prometheus, or I/O.

This module contains the mathematical and business logic that can be tested,
verified, and reused independently of infrastructure concerns (K8s, Prometheus, etc.).

For infrastructure adapters, see ppa.infrastructure.*
"""

from ppa.domain.feature_validation import (
    FEATURE_BOUNDS,
    validate_feature_bounds,
)
from ppa.domain.scaling import (
    calculate_replicas,
    calculate_replicas_fixed,
    calculate_replicas_old,
)
from ppa.domain.state import CRState

__all__ = [
    # Feature validation
    "FEATURE_BOUNDS",
    "validate_feature_bounds",
    # Scaling domain logic
    "calculate_replicas",
    "calculate_replicas_fixed",
    "calculate_replicas_old",
    # State management
    "CRState",
]
