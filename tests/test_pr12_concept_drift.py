# tests/test_pr12_concept_drift.py — Test concept drift detection
"""Test that concept drift is properly detected and tracked."""

import sys
import time
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Note: Full Predictor tests require TFLite model files, so we test the drift detection logic
# in isolation by mocking prediction_history and actual_history


class MockPredictor:
    """Mock predictor with just the drift detection logic for testing."""

    def __init__(self):
        from collections import deque
        self.prediction_history = deque(maxlen=60)
        self.actual_history = deque(maxlen=60)
        self.concept_drift_detected = False
        self.last_drift_check_time = 0.0

    def track_prediction_accuracy(self, predicted_rps, actual_rps):
        """Track prediction vs actual RPS."""
        self.prediction_history.append(predicted_rps)
        self.actual_history.append(actual_rps)

    def check_concept_drift(self):
        """Detect concept drift by comparing predicted vs actual RPS."""
        import numpy as np
        import math

        current_time = time.time()

        # Only check every 5 minutes
        if current_time - self.last_drift_check_time < 300:
            return {'detected': self.concept_drift_detected, 'checked': False}

        self.last_drift_check_time = current_time

        if len(self.prediction_history) < 10 or len(self.actual_history) < 10:
            return {'detected': False, 'checked': True, 'reason': 'insufficient_history'}

        recent_predictions = list(self.prediction_history)[-10:]
        recent_actuals = list(self.actual_history)[-10:]

        if not recent_predictions or not recent_actuals:
            return {'detected': False, 'checked': True}

        # Calculate MAPE
        errors = []
        for pred, actual in zip(recent_predictions, recent_actuals):
            if actual > 0:
                error = abs(pred - actual) / actual * 100
                errors.append(error)

        if not errors:
            return {'detected': False, 'checked': True}

        mean_error_pct = np.mean(errors)
        drift_detected = mean_error_pct > 20
        severe_drift = mean_error_pct > 50

        if drift_detected or severe_drift:
            self.concept_drift_detected = True
        else:
            self.concept_drift_detected = False

        return {
            'detected': drift_detected,
            'error_pct': mean_error_pct,
            'severity': 'severe' if severe_drift else ('moderate' if drift_detected else 'normal'),
            'checked': True
        }


class TestConceptDriftDetection:
    """Test PR#12: Concept drift detection."""

    def test_no_drift_when_predictions_accurate(self):
        """When predictions match actual within 20%, no drift should be detected."""
        predictor = MockPredictor()

        # Add 10+ predictions that match actual very closely
        for i in range(12):
            actual = 100.0
            predicted = 102.0  # 2% error
            predictor.track_prediction_accuracy(predicted, actual)

        # Force time to pass drift check
        predictor.last_drift_check_time = 0

        drift_result = predictor.check_concept_drift()

        assert drift_result['checked'] == True
        assert drift_result['detected'] == False
        assert drift_result['error_pct'] < 20

    def test_moderate_drift_detected(self):
        """When predictions have 20-50% error, moderate drift should be detected."""
        predictor = MockPredictor()

        # Add predictions with ~30% error
        for i in range(12):
            actual = 100.0
            predicted = 130.0  # 30% error
            predictor.track_prediction_accuracy(predicted, actual)

        predictor.last_drift_check_time = 0

        drift_result = predictor.check_concept_drift()

        assert drift_result['checked'] == True
        assert drift_result['detected'] == True
        assert drift_result['error_pct'] > 20
        assert drift_result['severity'] == 'moderate'

    def test_severe_drift_detected(self):
        """When predictions have >50% error, severe drift should be detected."""
        predictor = MockPredictor()

        # Add predictions with ~60% error
        for i in range(12):
            actual = 100.0
            predicted = 160.0  # 60% error
            predictor.track_prediction_accuracy(predicted, actual)

        predictor.last_drift_check_time = 0

        drift_result = predictor.check_concept_drift()

        assert drift_result['checked'] == True
        assert drift_result['detected'] == True
        assert drift_result['error_pct'] > 50
        assert drift_result['severity'] == 'severe'

    def test_insufficient_history_skips_check(self):
        """With <10 samples, drift check should return insufficient_history."""
        predictor = MockPredictor()

        # Add only 5 predictions
        for i in range(5):
            predictor.track_prediction_accuracy(100.0, 100.0)

        predictor.last_drift_check_time = 0

        drift_result = predictor.check_concept_drift()

        assert drift_result['checked'] == True
        assert drift_result['detected'] == False
        assert 'reason' in drift_result

    def test_check_throttling_prevents_spam(self):
        """Drift checks should be throttled to once per 5 minutes."""
        predictor = MockPredictor()

        # Add enough history
        for i in range(12):
            predictor.track_prediction_accuracy(100.0, 100.0)

        predictor.last_drift_check_time = time.time()

        # First check should be skipped (too soon)
        drift_result = predictor.check_concept_drift()
        assert drift_result['checked'] == False

        # Force time to pass
        predictor.last_drift_check_time = time.time() - 300

        # Second check should proceed
        drift_result = predictor.check_concept_drift()
        assert drift_result['checked'] == True

    def test_zero_actual_values_skipped(self):
        """Predictions against zero actual should be skipped in MAPE calculation."""
        predictor = MockPredictor()

        # Add predictions with some zero actuals
        for i in range(5):
            predictor.track_prediction_accuracy(100.0, 0.0)  # Can't calculate error
        for i in range(7):
            predictor.track_prediction_accuracy(100.0, 100.0)  # Perfect prediction

        predictor.last_drift_check_time = 0

        drift_result = predictor.check_concept_drift()

        assert drift_result['checked'] == True
        assert drift_result['detected'] == False

    def test_drift_state_transitions(self):
        """Test transitions between drift detected and not detected."""
        predictor = MockPredictor()

        # Start with accurate predictions
        for i in range(12):
            predictor.track_prediction_accuracy(100.0, 100.0)

        predictor.last_drift_check_time = 0
        result1 = predictor.check_concept_drift()

        assert result1['detected'] == False
        assert predictor.concept_drift_detected == False

        # Now clear and add bad predictions
        predictor.prediction_history.clear()
        predictor.actual_history.clear()
        for i in range(12):
            predictor.track_prediction_accuracy(150.0, 100.0)  # 50% error

        predictor.last_drift_check_time = 0
        result2 = predictor.check_concept_drift()

        assert result2['detected'] == True
        assert predictor.concept_drift_detected == True
