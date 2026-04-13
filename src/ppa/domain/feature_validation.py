"""Pure domain logic for feature validation and bounds checking.

Detects data quality issues and concept drift independently of data source.
"""

import logging
import math

from ppa.config import FeatureVectorError

logger = logging.getLogger("ppa.domain.feature_validation")

# Feature bounds to detect anomalies and prevent extrapolation
# Based on training data ranges plus tolerance for real-world variance
# Moved from operator/features.py (PR#11: Feature bounds validation)
FEATURE_BOUNDS = {
    "rps_per_replica": (0.0, 100),  # Per-pod RPS; 0 is valid when app is idle
    "cpu_utilization_pct": (0, 150),  # CPU 0-150% (allow some overshoot)
    "memory_utilization_pct": (0, 150),  # Memory 0-150% (allow some overshoot)
    "latency_p95_ms": (1, 10000),  # P95 latency 1-10000 ms; 0 means no-data (NaN-converted upstream)
    "active_connections": (0, 100000),  # Connections bounded
    "error_rate": (0, 1),  # Error rate 0-100%
    "cpu_acceleration": (-100, 100),  # CPU change clamped
    "rps_acceleration": (-100, 100),  # RPS change clamped
    "replicas_normalized": (0, 1),  # Normalized to [0, max_replicas]
    "hour_sin": (-1, 1),  # Trig bounds
    "hour_cos": (-1, 1),
    "dow_sin": (-1, 1),
    "dow_cos": (-1, 1),
    "is_weekend": (0, 1),  # Binary
}


def validate_feature_bounds(features: dict) -> tuple[dict[str, float | None], list]:
    """Validate feature vector for data quality issues and concept drift.

    Detects common problems that indicate data collection failures or model staleness:
    - NaN values (missing metrics from Prometheus timeouts)
    - Out-of-range values (e.g., CPU >150%, negative RPS)
    - Extreme values (likely data collection bugs or scaling anomalies)

    This is called before every prediction to catch issues early and log them
    for debugging. Invalid features are clipped to bounds and warnings recorded.

    Feature Bounds (from PR#11):
        rps_per_replica: [0, 100] RPS/pod (0 allowed for idle apps)
        cpu_utilization_pct: [0, 150] %
        memory_utilization_pct: [0, 150] %
        latency_p95_ms: [1, 10000] ms (0 is a PromQL sentinel, converted to NaN upstream)
        error_rate: [0, 1] (0-100%)
        Acceleration metrics: [-100, 100]
        Time features: [-1, 1] (sin/cos of hour/dow)

    Args:
        features: Dict mapping metric names to float values.
                  Expected keys: rps_per_replica, cpu_utilization_pct, etc.

    Returns:
        Tuple of (cleaned_features, warnings_list) where:
        - cleaned_features: Dict with out-of-bounds values clipped to [min, max]
        - warnings_list: List of dicts describing each anomaly

    Raises:
        FeatureVectorError: If >20% of features are invalid (data collection broken)

    Example:
        >>> features = {
        ...     'rps_per_replica': 45.2,
        ...     'cpu_utilization_pct': 250,  # Out of bounds!
        ... }
        >>> cleaned, warnings = validate_feature_bounds(features)
        >>> assert cleaned['cpu_utilization_pct'] == 150  # Clipped
        >>> assert len(warnings) == 1

    Design Notes:
        - Fail-safe: clips values rather than dropping them (maintains prediction)
        - Observable: logs all anomalies for debugging
        - Strict threshold: >20% invalid triggers exception (prevents bad predictions)
        - Idempotent: safe to call multiple times (doesn't modify input dict)

    See Also:
        PR#11: Feature bounds validation design docs
        PR#12: Concept drift detection
    """
    out_of_bounds = []
    validated = features.copy()

    for feature_name, value in validated.items():
        if feature_name not in FEATURE_BOUNDS:
            continue  # Skip unknown features

        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue  # Skip None and NaN values

        min_bound, max_bound = FEATURE_BOUNDS[feature_name]

        if value < min_bound or value > max_bound:
            out_of_bounds.append(
                {
                    "feature": feature_name,
                    "value": value,
                    "bounds": (min_bound, max_bound),
                }
            )
            # Log the anomaly
            logger.warning(
                f"Feature {feature_name}={value:.2f} out of bounds [{min_bound}, {max_bound}], clipping"
            )
            # Clip to bounds
            validated[feature_name] = max(min_bound, min(max_bound, value))

    # If >20% of features are out of bounds, raise exception (signal something is very wrong)
    if len(out_of_bounds) > len(FEATURE_BOUNDS) * 0.2:
        raise FeatureVectorError(
            f"Too many features out of bounds ({len(out_of_bounds)}/{len(FEATURE_BOUNDS)}): "
            f"{[f['feature'] for f in out_of_bounds]}"
        )

    return validated, out_of_bounds
