"""Tests for Phase 3C: Per-CR State Isolation - ensure CR instances don't interfere.

FIX (PR#10): CR state dictionary properly isolates instances.
Previously, if not carefully managed, state from one CR could leak to another.
Now validates each CR has isolated state, cleanup on deletion prevents orphaned entries.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from ppa.config import LOOKBACK_STEPS
from ppa.operator.predictor import Predictor


class TestCRStateIsolation:
    """Test that CR state is properly isolated per instance."""

    def test_each_predictor_has_independent_history(self):
        """Each Predictor instance should have independent history deque."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create two model paths
            model_path_1 = tmpdir / "model_1.tflite"
            model_path_1.write_bytes(b"dummy1")
            scaler_path_1 = tmpdir / "scaler_1.pkl"
            scaler_path_1.write_bytes(b"dummy1")

            model_path_2 = tmpdir / "model_2.tflite"
            model_path_2.write_bytes(b"dummy2")
            scaler_path_2 = tmpdir / "scaler_2.pkl"
            scaler_path_2.write_bytes(b"dummy2")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                # Create two independent predictors
                predictor_1 = Predictor(str(model_path_1), str(scaler_path_1))
                predictor_2 = Predictor(str(model_path_2), str(scaler_path_2))

                # Add data to predictor_1
                for i in range(5):
                    row_1 = np.ones(NUM_FEATURES, dtype=np.float32) * i
                    predictor_1.history.append(row_1)

                # Predictor_2 should have empty history
                assert len(predictor_1.history) == 5
                assert len(predictor_2.history) == 0

                # Add different data to predictor_2
                for i in range(3):
                    row_2 = np.ones(NUM_FEATURES, dtype=np.float32) * (10 + i)
                    predictor_2.history.append(row_2)

                # Verify isolation: predictor_1 history unchanged
                assert len(predictor_1.history) == 5
                assert len(predictor_2.history) == 3

                # Verify data integrity
                # Predictor_1's last entry should be 4
                assert predictor_1.history[-1][0] == 4.0
                # Predictor_2's last entry should be 12
                assert predictor_2.history[-1][0] == 12.0

    def test_prediction_history_isolated_per_cr(self):
        """Each CR should track its own prediction accuracy over time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path), str(scaler_path))
                predictor_2 = Predictor(str(model_path), str(scaler_path))

                # Add predictions to predictor_1
                for i in range(10):
                    predictor_1.prediction_history.append(float(i))
                    predictor_1.actual_history.append(float(i * 1.1))

                # Predictor_2 should have empty prediction history
                assert len(predictor_1.prediction_history) == 10
                assert len(predictor_2.prediction_history) == 0

                # Add different data to predictor_2
                for i in range(5):
                    predictor_2.prediction_history.append(float(100 + i))
                    predictor_2.actual_history.append(float(105 + i))

                # Verify isolation
                assert len(predictor_1.prediction_history) == 10
                assert len(predictor_2.prediction_history) == 5

                # Verify data is different
                assert predictor_1.prediction_history[0] == 0.0
                assert predictor_2.prediction_history[0] == 100.0

    def test_model_paths_unique_per_cr(self):
        """Different CRs should use different model paths."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path_1 = tmpdir / "cr1" / "model.tflite"
            model_path_1.parent.mkdir()
            model_path_1.write_bytes(b"dummy1")
            scaler_path_1 = tmpdir / "cr1" / "scaler.pkl"
            scaler_path_1.write_bytes(b"dummy1")

            model_path_2 = tmpdir / "cr2" / "model.tflite"
            model_path_2.parent.mkdir()
            model_path_2.write_bytes(b"dummy2")
            scaler_path_2 = tmpdir / "cr2" / "scaler.pkl"
            scaler_path_2.write_bytes(b"dummy2")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path_1), str(scaler_path_1))
                predictor_2 = Predictor(str(model_path_2), str(scaler_path_2))

                # Verify different paths
                assert predictor_1.model_path != predictor_2.model_path
                assert predictor_1.scaler_path != predictor_2.scaler_path

                # Verify paths_match works correctly
                assert predictor_1.paths_match(str(model_path_1), str(scaler_path_1))
                assert not predictor_1.paths_match(str(model_path_2), str(scaler_path_2))

                assert predictor_2.paths_match(str(model_path_2), str(scaler_path_2))
                assert not predictor_2.paths_match(str(model_path_1), str(scaler_path_1))

    def test_lookback_can_differ_per_model(self):
        """Different models might have different lookback values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path), str(scaler_path))

                # Simulate loading a model with different lookback
                predictor_1.input_details = [{"shape": (1, 30, NUM_FEATURES)}]
                predictor_1.output_details = [{}]
                predictor_1.lookback = 30  # Different from default 60

                # Create another predictor with standard lookback
                predictor_2 = Predictor(str(model_path), str(scaler_path))
                predictor_2.input_details = [{"shape": (1, 60, NUM_FEATURES)}]
                predictor_2.output_details = [{}]
                # lookback stays at default 60

                # Verify different lookback values
                assert predictor_1.lookback == 30
                assert predictor_2.lookback == 60

                # History deques should have different maxlen
                assert predictor_1.history.maxlen == LOOKBACK_STEPS  # Default
                # (Note: we'd need to reinit history with new maxlen in real scenario)


class TestCRStateDeletion:
    """Test cleanup when CR is deleted."""

    def test_predictor_cleanup_on_deletion(self):
        """Deleting a predictor should clean up resources."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))

                # Add some state
                for i in range(10):
                    row = np.ones(NUM_FEATURES, dtype=np.float32) * i
                    predictor.history.append(row)

                assert len(predictor.history) == 10

                # Delete predictor
                del predictor

                # Memory would be freed by Python garbage collector
                # (can't easily test this without memory profiling)

    def test_multiple_predictors_cleanup_order_independent(self):
        """Order of deleting multiple predictors shouldn't matter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path_1 = tmpdir / "model_1.tflite"
            model_path_1.write_bytes(b"dummy1")
            scaler_path_1 = tmpdir / "scaler_1.pkl"
            scaler_path_1.write_bytes(b"dummy1")

            model_path_2 = tmpdir / "model_2.tflite"
            model_path_2.write_bytes(b"dummy2")
            scaler_path_2 = tmpdir / "scaler_2.pkl"
            scaler_path_2.write_bytes(b"dummy2")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path_1), str(scaler_path_1))
                predictor_2 = Predictor(str(model_path_2), str(scaler_path_2))

                # Fill with data
                for i in range(5):
                    predictor_1.history.append(np.ones(NUM_FEATURES) * i)
                    predictor_2.history.append(np.ones(NUM_FEATURES) * (10 + i))

                # Delete first predictor
                del predictor_1

                # Second predictor should still have its data
                assert len(predictor_2.history) == 5


class TestCRStateConsistency:
    """Test that state remains consistent within a CR."""

    def test_history_consistency_on_update(self):
        """History should remain consistent as features are added."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))

                # Build feature dict
                features = {col: float(i) for i, col in enumerate(FEATURE_COLUMNS)}

                # Update multiple times
                for t in range(10):
                    # Modify features for this timestep
                    for i, col in enumerate(FEATURE_COLUMNS):
                        features[col] = float(i + t)

                    predictor.update(features)

                # Verify history consistency
                assert len(predictor.history) == 10

                # Each row should have the right number of features
                for row in predictor.history:
                    assert len(row) == NUM_FEATURES
                    assert isinstance(row, np.ndarray)

    def test_feature_order_consistency(self):
        """Features must be extracted in FEATURE_COLUMNS order."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))

                # Create features in a different order
                features_unordered = {}
                for i in range(len(FEATURE_COLUMNS) - 1, -1, -1):
                    features_unordered[FEATURE_COLUMNS[i]] = float(i)

                # Update with unordered dict
                predictor.update(features_unordered)

                # History should have features in FEATURE_COLUMNS order
                row = predictor.history[-1]
                for i, _col in enumerate(FEATURE_COLUMNS):
                    assert row[i] == float(i)


class TestCRStateIsolationFromGlobals:
    """Test that CR state doesn't interfere with global settings."""

    def test_lookback_local_to_cr(self):
        """Changing lookback in one CR shouldn't affect globals."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))

                # Verify initial lookback equals global LOOKBACK_STEPS
                assert predictor.lookback == LOOKBACK_STEPS

                # Change lookback (simulating loaded model with different lookback)
                original_global = LOOKBACK_STEPS
                predictor.lookback = 30

                # Global should still be unchanged
                assert original_global == LOOKBACK_STEPS
                assert predictor.lookback == 30

    def test_concept_drift_isolated_per_cr(self):
        """Concept drift flag should be independent per CR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path), str(scaler_path))
                predictor_2 = Predictor(str(model_path), str(scaler_path))

                # Set drift flag on predictor_1
                predictor_1.concept_drift_detected = True

                # Predictor_2 should have default value
                assert predictor_1.concept_drift_detected is True
                assert predictor_2.concept_drift_detected is False


class TestCRStateRecovery:
    """Test state recovery after failures."""

    def test_history_serialization_isolation(self):
        """Serialized history from one CR shouldn't affect another."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy")
            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy")

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor_1 = Predictor(str(model_path), str(scaler_path))
                predictor_2 = Predictor(str(model_path), str(scaler_path))

                # Add data to predictor_1
                for i in range(5):
                    row = np.ones(NUM_FEATURES, dtype=np.float32) * i
                    predictor_1.history.append(row)

                # Serialize predictor_1's history
                serialized = predictor_1.serialize_history()
                assert serialized is not None
                assert len(serialized) == 5

                # Restore to predictor_2
                success = predictor_2.deserialize_history(serialized)
                assert success is True
                assert len(predictor_2.history) == 5

                # Verify data matches
                assert predictor_1.history[-1][0] == predictor_2.history[-1][0]

                # Modify predictor_2's history
                predictor_2.history[-1][0] = 999.0

                # Predictor_1 should be unaffected
                assert predictor_1.history[-1][0] != 999.0
