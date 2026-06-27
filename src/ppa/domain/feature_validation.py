"""Pure domain logic for feature validation and bounds checking.

Detects data quality issues and concept drift independently of data source.
"""

import logging
import math

from ppa.config import FeatureVectorError

logger = logging.getLogger("ppa.domain.feature_validation")

# Feature bounds — v3 foundation model schema (normalized features)
FEATURE_BOUNDS = {
    "normalized_rps": (0.0, 1.0),  # RPS / rolling max RPS
    "cpu_utilization_pct": (0, 150),  # CPU 0-150% (allow some overshoot)
    "memory_utilization_pct": (0, 150),  # Memory 0-150% (allow some overshoot)
    "latency_normalized": (0.0, 1.0),  # P95 latency / rolling max latency
    "error_rate": (0, 1),  # Error rate 0-100%
    "cpu_acceleration": (-1, 1),  # CPU change (normalized diff)
    "rps_acceleration": (-1, 1),  # RPS change (normalized diff)
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
    - Out-of-range values (e.g., CPU >150%, negative normalized RPS)
    - Extreme values (likely data collection bugs or scaling anomalies)

    Feature Bounds (v3 foundation model):
        normalized_rps: [0, 1] (RPS / rolling max)
        cpu_utilization_pct: [0, 150] %
        memory_utilization_pct: [0, 150] %
        latency_normalized: [0, 1] (latency / rolling max)
        error_rate: [0, 1] (0-100%)
        Acceleration metrics: [-1, 1] (normalized)
        Time features: [-1, 1] (sin/cos of hour/dow)

    Args:
        features: Dict mapping metric names to float values.

    Returns:
        Tuple of (cleaned_features, warnings_list) where:
        - cleaned_features: Dict with out-of-bounds values clipped to [min, max]
        - warnings_list: List of dicts describing each anomaly

    Raises:
        FeatureVectorError: If >20% of features are invalid (data collection broken)
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
            logger.warning(
                f"Feature {feature_name}={value:.2f} out of bounds [{min_bound}, {max_bound}], clipping"
            )
            validated[feature_name] = max(min_bound, min(max_bound, value))

    if len(out_of_bounds) > len(FEATURE_BOUNDS) * 0.2:
        raise FeatureVectorError(
            f"Too many features out of bounds ({len(out_of_bounds)}/{len(FEATURE_BOUNDS)}): "
            f"{[f['feature'] for f in out_of_bounds]}"
        )

    return validated, out_of_bounds
