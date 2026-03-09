# tests/test_train.py — Unit tests for model/train.py
import os
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
from model.train import create_dataset_from_segments, train_model, LOOKBACK_STEPS
from sklearn.preprocessing import MinMaxScaler


def _make_synthetic_df(n_rows=200, with_segments=True):
    """Generate a synthetic DataFrame matching the expected schema."""
    rng = np.random.default_rng(42)
    data = {col: rng.random(n_rows) * 100 for col in FEATURE_COLUMNS}
    for col in TARGET_COLUMNS:
        data[col] = rng.random(n_rows) * 50
    if with_segments:
        data["segment_id"] = np.where(np.arange(n_rows) < n_rows // 2, 0, 1)
    data["timestamp"] = pd.date_range("2026-01-01", periods=n_rows, freq="30s")
    df = pd.DataFrame(data).set_index("timestamp")
    return df


def _save_synthetic_csv(tmp_dir, n_rows=200, with_segments=True):
    """Write synthetic data to a temp CSV and return the path."""
    df = _make_synthetic_df(n_rows, with_segments)
    path = os.path.join(tmp_dir, "training_data.csv")
    df.to_csv(path)
    return path


# ── create_dataset_from_segments tests ──────────────────────────────────

class TestCreateDatasetFromSegments:
    def test_window_shape(self):
        """Windows should have shape (n, lookback, n_features)."""
        df = _make_synthetic_df(100, with_segments=False)
        scaler = MinMaxScaler().fit(df[FEATURE_COLUMNS])
        X, y = create_dataset_from_segments(
            df, FEATURE_COLUMNS, TARGET_COLUMNS[0], scaler, LOOKBACK_STEPS
        )
        assert X.shape[1] == LOOKBACK_STEPS
        assert X.shape[2] == len(FEATURE_COLUMNS)
        assert len(X) == len(y)

    def test_expected_window_count_no_segments(self):
        """Without segments: n_windows = n_rows - lookback."""
        n = 50
        df = _make_synthetic_df(n, with_segments=False)
        scaler = MinMaxScaler().fit(df[FEATURE_COLUMNS])
        X, y = create_dataset_from_segments(
            df, FEATURE_COLUMNS, TARGET_COLUMNS[0], scaler, LOOKBACK_STEPS
        )
        assert len(X) == n - LOOKBACK_STEPS

    def test_segment_boundary_respected(self):
        """Segments should not bleed into each other."""
        n = 60
        df = _make_synthetic_df(n, with_segments=True)
        # Each segment has n//2 = 30 rows → 30 - lookback windows each
        scaler = MinMaxScaler().fit(df[FEATURE_COLUMNS])
        X, y = create_dataset_from_segments(
            df, FEATURE_COLUMNS, TARGET_COLUMNS[0], scaler, LOOKBACK_STEPS
        )
        expected = 2 * (n // 2 - LOOKBACK_STEPS)
        assert len(X) == expected

    def test_scaled_values_bounded(self):
        """Scaled features should be in [0, 1] range."""
        df = _make_synthetic_df(100, with_segments=False)
        scaler = MinMaxScaler().fit(df[FEATURE_COLUMNS])
        X, _ = create_dataset_from_segments(
            df, FEATURE_COLUMNS, TARGET_COLUMNS[0], scaler, LOOKBACK_STEPS
        )
        assert X.min() >= -0.01  # Allow tiny float imprecision
        assert X.max() <= 1.01


# ── train_model integration tests ──────────────────────────────────────

class TestTrainModel:
    def test_model_trains_and_produces_artifacts(self):
        """Full training run on synthetic data produces expected files."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _save_synthetic_csv(tmp, n_rows=200)
            output_dir = os.path.join(tmp, "artifacts")
            target = TARGET_COLUMNS[0]

            result = train_model(
                csv_path=csv_path,
                lookback=LOOKBACK_STEPS,
                epochs=2,
                target_col=target,
                test_split=0.1,
                output_dir=output_dir,
            )

            assert result is not None
            assert os.path.exists(result["artifact_paths"]["model"])
            assert os.path.exists(result["artifact_paths"]["scaler"])
            assert os.path.exists(result["artifact_paths"]["target_scaler"])
            assert os.path.exists(result["artifact_paths"]["meta"])

    def test_different_targets_produce_different_artifacts(self):
        """Each horizon should get its own named artifacts."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _save_synthetic_csv(tmp, n_rows=200)
            output_dir = os.path.join(tmp, "artifacts")

            for target in [TARGET_COLUMNS[0], TARGET_COLUMNS[1]]:
                result = train_model(
                    csv_path=csv_path,
                    lookback=LOOKBACK_STEPS,
                    epochs=2,
                    target_col=target,
                    output_dir=output_dir,
                )
                assert result is not None
                assert target in result["artifact_paths"]["model"]

            # Both model files should exist
            files = os.listdir(output_dir)
            assert any(TARGET_COLUMNS[0] in f and f.endswith(".keras") for f in files)
            assert any(TARGET_COLUMNS[1] in f and f.endswith(".keras") for f in files)

    def test_split_metadata_correct(self):
        """split_meta JSON should contain valid split info."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _save_synthetic_csv(tmp, n_rows=200)
            output_dir = os.path.join(tmp, "artifacts")

            result = train_model(
                csv_path=csv_path, epochs=2,
                target_col=TARGET_COLUMNS[0],
                output_dir=output_dir,
            )

            with open(result["artifact_paths"]["meta"]) as f:
                meta = json.load(f)

            assert meta["target_col"] == TARGET_COLUMNS[0]
            assert meta["train_size"] > 0
            assert meta["val_size"] > 0
            assert meta["test_size"] > 0
            assert meta["train_size"] + meta["val_size"] + meta["test_size"] == meta["total_windows"]

    def test_returns_none_for_missing_csv(self):
        result = train_model(csv_path="/nonexistent/path.csv", epochs=1)
        assert result is None

    def test_returns_none_for_invalid_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _save_synthetic_csv(tmp)
            result = train_model(csv_path=csv_path, epochs=1, target_col="not_a_column")
            assert result is None

    def test_model_output_shape(self):
        """Model output should be (batch, 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = _save_synthetic_csv(tmp, n_rows=200)
            output_dir = os.path.join(tmp, "artifacts")

            result = train_model(
                csv_path=csv_path, epochs=2,
                target_col=TARGET_COLUMNS[0],
                output_dir=output_dir,
            )

            model = result["model"]
            dummy_input = np.random.rand(5, LOOKBACK_STEPS, len(FEATURE_COLUMNS)).astype(np.float32)
            output = model.predict(dummy_input, verbose=0)
            assert output.shape == (5, 1)
