# tests/test_evaluate.py — Unit tests for model/evaluate.py
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from model.evaluate import (
    compute_mape,
    compute_mae,
    compute_rmse,
    rps_to_replicas,
    compute_scaling_stats,
    evaluate_model,
)
from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
from common.constants import CAPACITY_PER_POD
from model.train import train_model, LOOKBACK_STEPS


def _make_synthetic_df(n_rows=200):
    rng = np.random.default_rng(42)
    data = {col: rng.random(n_rows) * 100 for col in FEATURE_COLUMNS}
    for col in TARGET_COLUMNS:
        data[col] = rng.random(n_rows) * 50
    data["segment_id"] = np.zeros(n_rows, dtype=int)
    data["timestamp"] = pd.date_range("2026-01-01", periods=n_rows, freq="30s")
    return pd.DataFrame(data).set_index("timestamp")


# ── Metric computation tests ───────────────────────────────────────────

class TestMetrics:
    def test_mape_perfect_prediction(self):
        y = np.array([10.0, 20.0, 30.0])
        assert compute_mape(y, y) == pytest.approx(0.0)

    def test_mape_known_value(self):
        y_true = np.array([100.0, 200.0, 300.0])
        y_pred = np.array([110.0, 190.0, 330.0])
        # Errors: 10%, 5%, 10% → mean = 8.33%
        expected = (10 / 100 + 10 / 200 + 30 / 300) / 3 * 100
        assert compute_mape(y_true, y_pred) == pytest.approx(expected, rel=1e-4)

    def test_mape_ignores_zero_actuals(self):
        y_true = np.array([0.0, 100.0, 200.0])
        y_pred = np.array([50.0, 110.0, 190.0])
        # Only indices 1,2 count: 10%, 5% → mean = 7.5%
        expected = (10 / 100 + 10 / 200) / 2 * 100
        assert compute_mape(y_true, y_pred) == pytest.approx(expected, rel=1e-4)

    def test_mae_known_value(self):
        y_true = np.array([1.0, 2.0, 3.0])
        y_pred = np.array([1.5, 2.5, 2.5])
        assert compute_mae(y_true, y_pred) == pytest.approx(0.5)

    def test_rmse_known_value(self):
        y_true = np.array([1.0, 2.0, 3.0])
        y_pred = np.array([1.0, 2.0, 5.0])
        # Errors: 0, 0, 4 → MSE = 4/3 → RMSE = sqrt(4/3)
        expected = np.sqrt(4 / 3)
        assert compute_rmse(y_true, y_pred) == pytest.approx(expected, rel=1e-4)

    def test_rmse_perfect(self):
        y = np.array([1.0, 2.0, 3.0])
        assert compute_rmse(y, y) == pytest.approx(0.0)


# ── HPA comparison tests ──────────────────────────────────────────────

class TestReplicaConversion:
    def test_basic_conversion(self):
        rps = np.array([100.0, 150.0, 200.0])
        replicas = rps_to_replicas(rps, capacity=50.0, min_r=2, max_r=20)
        np.testing.assert_array_equal(replicas, [2, 3, 4])

    def test_min_clamp(self):
        rps = np.array([10.0])
        replicas = rps_to_replicas(rps, capacity=50.0, min_r=2, max_r=20)
        assert replicas[0] == 2  # ceil(10/50)=1, clamped to 2

    def test_max_clamp(self):
        rps = np.array([5000.0])
        replicas = rps_to_replicas(rps, capacity=50.0, min_r=2, max_r=20)
        assert replicas[0] == 20  # ceil(5000/50)=100, clamped to 20

    def test_zero_rps(self):
        rps = np.array([0.0])
        replicas = rps_to_replicas(rps, capacity=50.0, min_r=2, max_r=20)
        assert replicas[0] == 2  # ceil(0)=0, clamped to 2


class TestScalingStats:
    def test_perfect_provisioning(self):
        rps = np.array([100.0, 200.0, 300.0])
        # Exactly right: 2, 4, 6 replicas at 50 cap
        replicas = np.array([2, 4, 6])
        stats = compute_scaling_stats(rps, replicas, 50.0, "test")
        assert stats["test_under_prov_pct"] == pytest.approx(0.0)
        assert stats["test_wasted_capacity_avg"] == pytest.approx(0.0)

    def test_over_provisioned(self):
        rps = np.array([50.0, 50.0])
        replicas = np.array([5, 5])  # 250 capacity for 50 rps
        stats = compute_scaling_stats(rps, replicas, 50.0, "test")
        assert stats["test_over_prov_pct"] == pytest.approx(100.0)
        assert stats["test_wasted_capacity_avg"] == pytest.approx(200.0)


# ── Full evaluate_model integration test ───────────────────────────────

class TestEvaluateModel:
    def test_end_to_end_evaluation(self):
        """Train on synthetic data, then evaluate — should produce metrics + files."""
        with tempfile.TemporaryDirectory() as tmp:
            # Write synthetic CSV
            df = _make_synthetic_df(300)
            csv_path = os.path.join(tmp, "data.csv")
            df.to_csv(csv_path)
            output_dir = os.path.join(tmp, "artifacts")
            target = TARGET_COLUMNS[0]

            # Train first
            train_result = train_model(
                csv_path=csv_path, epochs=2,
                target_col=target, output_dir=output_dir,
            )
            assert train_result is not None

            # Evaluate
            eval_result = evaluate_model(
                model_path=train_result["artifact_paths"]["model"],
                scaler_path=train_result["artifact_paths"]["scaler"],
                csv_path=csv_path,
                target_col=target,
                output_dir=output_dir,
                meta_path=train_result["artifact_paths"]["meta"],
            )

            assert eval_result is not None
            assert "mape" in eval_result
            assert "mae" in eval_result
            assert "rmse" in eval_result
            assert eval_result["test_samples"] > 0

            # Check files created
            assert os.path.exists(os.path.join(output_dir, f"eval_pred_vs_actual_{target}.png"))
            assert os.path.exists(os.path.join(output_dir, f"eval_ppa_vs_hpa_{target}.png"))
            assert os.path.exists(os.path.join(output_dir, f"eval_summary_{target}.json"))

    def test_returns_none_for_missing_model(self):
        result = evaluate_model(
            model_path="/nonexistent/model.keras",
            scaler_path="/nonexistent/scaler.pkl",
            csv_path="/nonexistent/data.csv",
        )
        assert result is None
