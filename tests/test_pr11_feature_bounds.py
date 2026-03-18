# tests/test_pr11_feature_bounds.py — Test feature bounds validation
"""Test that feature bounds validation prevents out-of-range values and detects anomalies."""

import sys
import math
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from operator.features import validate_feature_bounds, FEATURE_BOUNDS
from config import FeatureVectorException


class TestFeatureBoundsValidation:
    """Test PR#11: Feature bounds checking to prevent extrapolation."""

    def test_valid_features_pass_unchanged(self):
        """Valid features should pass through unchanged."""
        features = {
            'rps_per_replica': 5.0,
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 40.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert len(oob) == 0
        for key in features:
            assert validated[key] == features[key]

    def test_out_of_bounds_clipped_to_upper_limit(self):
        """Features above max bound should be clipped."""
        features = {
            'rps_per_replica': 200.0,  # Max is 100
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 50.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert len(oob) == 1
        assert validated['rps_per_replica'] == 100.0  # Clipped to max
        assert oob[0]['feature'] == 'rps_per_replica'
        assert oob[0]['value'] == 200.0

    def test_out_of_bounds_clipped_to_lower_limit(self):
        """Features below min bound should be clipped."""
        features = {
            'rps_per_replica': 0.001,  # Min is 0.01
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 50.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert len(oob) == 1
        assert validated['rps_per_replica'] == 0.01  # Clipped to min
        assert oob[0]['feature'] == 'rps_per_replica'

    def test_nan_features_skipped(self):
        """NaN values should be skipped (not cause bounds violation)."""
        features = {
            'rps_per_replica': math.nan,
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 50.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert len(oob) == 0
        assert math.isnan(validated['rps_per_replica'])

    def test_too_many_oob_features_raises_exception(self):
        """If >20% of features are OOB, should raise exception."""
        # Create a dict with >20% out of bounds
        features = {
            'rps_per_replica': 200.0,      # OOB
            'cpu_utilization_pct': 200.0,  # OOB
            'memory_utilization_pct': 200.0,  # OOB
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        with pytest.raises(FeatureVectorException) as exc_info:
            validate_feature_bounds(features.copy())

        assert "Too many features out of bounds" in str(exc_info.value)

    def test_negative_rps_clipped_to_min(self):
        """Negative RPS should be clipped to min (0.01)."""
        features = {
            'rps_per_replica': -5.0,
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 50.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 0.5,
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert len(oob) == 1
        assert validated['rps_per_replica'] == 0.01

    def test_replicas_normalized_bounds(self):
        """Replicas normalized should be [0, 1]."""
        # Test value > 1
        features = {
            'rps_per_replica': 5.0,
            'cpu_utilization_pct': 50.0,
            'memory_utilization_pct': 50.0,
            'latency_p95_ms': 100.0,
            'active_connections': 1000,
            'error_rate': 0.01,
            'cpu_acceleration': 5.0,
            'rps_acceleration': 2.0,
            'replicas_normalized': 1.5,  # Invalid
            'hour_sin': 0.5,
            'hour_cos': 0.5,
            'dow_sin': 0.3,
            'dow_cos': 0.4,
            'is_weekend': 0.0,
        }

        validated, oob = validate_feature_bounds(features.copy())

        assert validated['replicas_normalized'] == 1.0  # Clipped to max
