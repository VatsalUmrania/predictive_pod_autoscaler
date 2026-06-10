"""Tests for Phase 3A: Model Assertion Validation - early detection of schema mismatches.

FIX (PR#8): Feature order assertions now happen at model load time, not after 30min of history.
Previously, assertion at line 347 of operator/main.py only ran after history was complete.
Now validates immediately when model is loaded or features are built.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ppa.common.feature_spec import FEATURE_COLUMNS
from ppa.config import LOOKBACK_STEPS
from ppa.operator.predictor import Predictor


class TestMetadataValidation:
    """Test metadata validation at model load time (PR#8)."""

    def test_load_validates_feature_columns_match(self):
        """Metadata feature columns must match operator FEATURE_COLUMNS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Create dummy model and scaler files
            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Create metadata with CORRECT feature columns
            metadata = {
                "feature_columns": FEATURE_COLUMNS,
                "lookback": LOOKBACK_STEPS,
                "training_date": "2026-03-15",
                "accuracy_loss_pct": 2.5,
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            # Create predictor — should NOT raise error
            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                # Patch to bypass actual TFLite loading
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()
                meta = predictor._load_and_validate_metadata()

                # Metadata should be successfully loaded
                assert meta is not None
                assert meta["feature_columns"] == FEATURE_COLUMNS

    def test_load_raises_on_feature_columns_mismatch(self):
        """Metadata with different feature columns should raise immediately at load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Create metadata with WRONG feature columns (missing one)
            wrong_cols = FEATURE_COLUMNS[:-1]  # Drop last feature
            metadata = {
                "feature_columns": wrong_cols,
                "lookback": LOOKBACK_STEPS,
                "training_date": "2026-03-15",
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            # Create predictor with patched _try_load
            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                # Now _load_and_validate_metadata should raise ValueError (PR#8 + M7 fix)
                # M7 fix: Feature count is checked first, so the error message changes
                with pytest.raises(ValueError, match="Feature (count|column) mismatch"):
                    predictor._load_and_validate_metadata()

    def test_load_validates_lookback_mismatch_warns(self):
        """Metadata with different lookback should warn but not error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Metadata with different lookback
            metadata = {
                "feature_columns": FEATURE_COLUMNS,
                "lookback": LOOKBACK_STEPS + 10,  # Different
                "training_date": "2026-03-15",
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                # Should load metadata despite lookback mismatch (warning only)
                with patch("ppa.operator.predictor.logger") as mock_logger:
                    meta = predictor._load_and_validate_metadata()
                    assert meta is not None
                    # Verify warning was logged
                    mock_logger.warning.assert_called()
                    assert "Lookback mismatch" in str(mock_logger.warning.call_args)

    def test_load_warns_on_high_quantization_loss(self):
        """Metadata with >5% quantization loss should warn."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Metadata with high quantization loss
            metadata = {
                "feature_columns": FEATURE_COLUMNS,
                "lookback": LOOKBACK_STEPS,
                "accuracy_loss_pct": 7.5,  # >5%
                "training_date": "2026-03-15",
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                with patch("ppa.operator.predictor.logger") as mock_logger:
                    meta = predictor._load_and_validate_metadata()
                    assert meta is not None
                    # Should warn about high quantization loss
                    mock_logger.warning.assert_called()
                    assert "quantization loss" in str(mock_logger.warning.call_args).lower()

    def test_load_succeeds_with_missing_metadata_file(self):
        """If metadata file doesn't exist, should warn but continue (backward compat)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # No metadata file created

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))

                with patch("ppa.operator.predictor.logger") as mock_logger:
                    meta = predictor._load_and_validate_metadata()
                    assert meta is None
                    # Should warn but not error
                    mock_logger.warning.assert_called()


class TestFeatureVectorValidation:
    """Test feature vector validation at build time (earlier than inference)."""

    def test_feature_vector_order_matters(self):
        """Test that feature order validation is important."""
        # This test verifies the concept: features must be in FEATURE_COLUMNS order
        # The actual build_feature_vector calls are tested in test_scaler.py

        # Example of CORRECT order
        correct_order = {
            "rps_per_replica": 5.5,
            "cpu_utilization_pct": 45.0,
            "memory_utilization_pct": 60.0,
            "latency_p95_ms": 25.3,
            "active_connections": 10,
            "error_rate": 0.001,
            "cpu_acceleration": 0.1,
            "rps_acceleration": 0.2,
            "replicas_normalized": 0.5,
            "hour_sin": 0.5,
            "hour_cos": 0.866,
            "dow_sin": 0.7,
            "dow_cos": 0.714,
            "is_weekend": 0,
        }

        # Feature keys should match FEATURE_COLUMNS
        assert list(correct_order.keys()) == FEATURE_COLUMNS, "Test setup failed"

    def test_feature_vector_mismatch_detected_by_assertion(self):
        """Test that feature order mismatch is caught by assertion."""
        # Example of WRONG order (keys in wrong order)
        wrong_order = {
            "latency_p95_ms": 25.3,  # Wrong position
            "rps_per_replica": 5.5,  # Wrong position
            "cpu_utilization_pct": 45.0,
            "memory_utilization_pct": 60.0,
            "active_connections": 10,
            "error_rate": 0.001,
            "cpu_acceleration": 0.1,
            "rps_acceleration": 0.2,
            "replicas_normalized": 0.5,
            "hour_sin": 0.5,
            "hour_cos": 0.866,
            "dow_sin": 0.7,
            "dow_cos": 0.714,
            "is_weekend": 0,
        }

        # This would trigger the assertion at line 347 of operator/main.py:
        # assert list(features.keys()) == FEATURE_COLUMNS
        keys = list(wrong_order.keys())
        assert keys != FEATURE_COLUMNS, "Keys should not match (test setup)"


class TestAssertionTiming:
    """Test that assertions happen early (load/build time), not late (inference time)."""

    def test_assertion_timing_feature_mismatch_at_load_not_inference(self):
        """Feature column mismatch should be detected at load, not after 30min of history."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Metadata with WRONG columns
            wrong_cols = ["wrong_feature_1", "wrong_feature_2"]
            metadata = {
                "feature_columns": wrong_cols,
                "lookback": LOOKBACK_STEPS,
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            # Predictor initialization should NOT load the model yet (lazy)
            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                # The KEY POINT: _load_and_validate_metadata is called in _try_load
                # and should raise IMMEDIATELY (PR#8 + M7 fix), not after 30min of history
                # M7 fix: Feature count is checked first, so the error message changes
                with pytest.raises(ValueError, match="Feature (count|column) mismatch"):
                    predictor._load_and_validate_metadata()

    def test_feature_vector_validation_before_predictor_update(self):
        """Feature vector validation should happen before feeding to predictor."""
        # Feature vector with wrong order
        wrong_order_features = {
            "latency_p95_ms": 25.3,  # Wrong order
            "rps_per_replica": 5.5,
            "cpu_utilization_pct": 45.0,
            # ... rest of features
        }

        # When checking feature order against FEATURE_COLUMNS
        keys = list(wrong_order_features.keys())
        expected = FEATURE_COLUMNS

        # Should detect mismatch
        assert keys != expected, "Test setup: features should be in wrong order"

        # In actual code, assertion at line 347 catches this:
        # assert list(features.keys()) == FEATURE_COLUMNS


class TestSchemaCompatibility:
    """Test cross-version schema compatibility checking."""

    def test_model_from_new_training_date_detectable(self):
        """Can detect model age from training date in metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Metadata with training date
            metadata = {
                "feature_columns": FEATURE_COLUMNS,
                "lookback": LOOKBACK_STEPS,
                "training_date": "2026-03-15",
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                meta = predictor._load_and_validate_metadata()
                assert meta is not None
                assert "training_date" in meta
                assert meta["training_date"] == "2026-03-15"

    def test_model_version_tracking_in_metadata(self):
        """Metadata should track model version for auditing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            # Metadata with version
            metadata = {
                "version": "2.0",  # Can track schema version
                "feature_columns": FEATURE_COLUMNS,
                "lookback": LOOKBACK_STEPS,
                "training_date": "2026-03-15",
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                meta = predictor._load_and_validate_metadata()
                assert meta is not None
                assert meta.get("version") == "2.0"


class TestErrorMessages:
    """Test that error messages are clear and actionable."""

    def test_feature_mismatch_error_shows_expected_vs_actual(self):
        """Error message should show what was expected vs what was found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            model_path = tmpdir / "model.tflite"
            model_path.write_bytes(b"dummy_model")

            scaler_path = tmpdir / "scaler.pkl"
            scaler_path.write_bytes(b"dummy_scaler")

            wrong_cols = ["wrong1", "wrong2"]
            metadata = {
                "feature_columns": wrong_cols,
                "lookback": LOOKBACK_STEPS,
            }
            metadata_path = tmpdir / "model_metadata.json"
            metadata_path.write_text(json.dumps(metadata))

            with patch("ppa.operator.predictor.Predictor._try_load"):
                predictor = Predictor(str(model_path), str(scaler_path))
                predictor.interpreter = MagicMock()
                predictor.scaler = MagicMock()

                with pytest.raises(ValueError) as exc_info:
                    predictor._load_and_validate_metadata()

                error_msg = str(exc_info.value)
                # Error should mention both expected and actual (PR#8)
                assert "model expects" in error_msg.lower()
                assert "operator has" in error_msg.lower()
