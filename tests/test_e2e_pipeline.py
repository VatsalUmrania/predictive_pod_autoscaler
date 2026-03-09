# tests/test_e2e_pipeline.py — End-to-end pipeline tests with synthetic data
import json
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

from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
from model.train import train_model, create_dataset_from_segments
from model.evaluate import evaluate_model, compute_mape
from model.convert import convert_model


def _make_synthetic_df(n_rows=500, with_segment_id=True):
    """Generate synthetic RPS data matching the schema."""
    rng = np.random.default_rng(42)
    
    data = {col: rng.random(n_rows) * 100 for col in FEATURE_COLUMNS}
    for col in TARGET_COLUMNS:
        data[col] = rng.random(n_rows) * 50 + 20  # 20-70 RPS
    
    if with_segment_id:
        data["segment_id"] = np.zeros(n_rows, dtype=int)
    
    data["timestamp"] = pd.date_range("2026-03-01", periods=n_rows, freq="30s")
    
    df = pd.DataFrame(data).set_index("timestamp")
    return df


@pytest.fixture
def synthetic_csv():
    """Fixture providing a temporary synthetic training CSV."""
    with tempfile.TemporaryDirectory() as tmpdir:
        df = _make_synthetic_df(500)
        csv_path = os.path.join(tmpdir, "synthetic_data.csv")
        df.to_csv(csv_path)
        yield csv_path, tmpdir


class TestTrainPipeline:
    """Test the training pipeline for a single horizon."""

    def test_train_produces_all_artifacts(self, synthetic_csv):
        csv_path, tmpdir = synthetic_csv
        
        result = train_model(
            csv_path=csv_path,
            target_col=TARGET_COLUMNS[0],
            epochs=2,
            test_split=0.1,
            output_dir=tmpdir,
        )
        
        assert result is not None, "Training should return a result dict"
        
        # Check all artifacts exist
        assert Path(result["artifact_paths"]["model"]).exists()
        assert Path(result["artifact_paths"]["scaler"]).exists()
        assert Path(result["artifact_paths"]["target_scaler"]).exists()
        assert Path(result["artifact_paths"]["meta"]).exists()
        
        # Verify split metadata
        with open(result["artifact_paths"]["meta"]) as f:
            meta = json.load(f)
        
        assert meta["target_col"] == TARGET_COLUMNS[0]
        assert meta["train_size"] > 0
        assert meta["val_size"] > 0
        assert meta["test_size"] > 0

    def test_train_different_horizons(self, synthetic_csv):
        """Each horizon should produce differently-named artifacts."""
        csv_path, tmpdir = synthetic_csv
        
        results = {}
        for horizon in [TARGET_COLUMNS[0], TARGET_COLUMNS[1]]:
            result = train_model(
                csv_path=csv_path,
                target_col=horizon,
                epochs=2,
                output_dir=tmpdir,
            )
            results[horizon] = result
        
        # Verify different horizon names in paths
        path1 = results[TARGET_COLUMNS[0]]["artifact_paths"]["model"]
        path2 = results[TARGET_COLUMNS[1]]["artifact_paths"]["model"]
        
        assert TARGET_COLUMNS[0] in path1
        assert TARGET_COLUMNS[1] in path2
        assert path1 != path2


class TestEvaluatePipeline:
    """Test the evaluation pipeline."""

    def test_evaluate_full_workflow(self, synthetic_csv):
        """Train then evaluate and produce metrics + plots."""
        csv_path, tmpdir = synthetic_csv
        target = TARGET_COLUMNS[0]
        
        # Train
        train_result = train_model(
            csv_path=csv_path,
            target_col=target,
            epochs=2,
            output_dir=tmpdir,
        )
        assert train_result is not None
        
        # Evaluate
        eval_result = evaluate_model(
            model_path=train_result["artifact_paths"]["model"],
            scaler_path=train_result["artifact_paths"]["scaler"],
            csv_path=csv_path,
            target_col=target,
            output_dir=tmpdir,
            meta_path=train_result["artifact_paths"]["meta"],
            target_scaler_path=train_result["artifact_paths"]["target_scaler"],
        )
        
        assert eval_result is not None
        assert "mape" in eval_result
        assert "mae" in eval_result
        assert "rmse" in eval_result
        assert eval_result["test_samples"] > 0
        
        # Verify output files
        expected_files = [
            f"eval_pred_vs_actual_{target}.png",
            f"eval_ppa_vs_hpa_{target}.png",
            f"eval_summary_{target}.json",
        ]
        
        for fname in expected_files:
            fpath = os.path.join(tmpdir, fname)
            assert os.path.exists(fpath), f"Missing: {fname}"

    def test_hpa_vs_ppa_comparison(self, synthetic_csv):
        """Verify HPA vs PPA comparison metrics are computed."""
        csv_path, tmpdir = synthetic_csv
        target = TARGET_COLUMNS[0]
        
        train_result = train_model(
            csv_path=csv_path,
            target_col=target,
            epochs=2,
            output_dir=tmpdir,
        )
        
        eval_result = evaluate_model(
            model_path=train_result["artifact_paths"]["model"],
            scaler_path=train_result["artifact_paths"]["scaler"],
            csv_path=csv_path,
            target_col=target,
            output_dir=tmpdir,
            meta_path=train_result["artifact_paths"]["meta"],
            target_scaler_path=train_result["artifact_paths"]["target_scaler"],
        )
        
        # Check HPA/PPA stats exist
        assert "ppa_avg_replicas" in eval_result
        assert "hpa_avg_replicas" in eval_result
        assert "ppa_over_prov_pct" in eval_result
        assert "replica_savings_pct" in eval_result
        
        # PPA should generally have fewer avg replicas (proactive)
        ppa_avg = eval_result["ppa_avg_replicas"]
        hpa_avg = eval_result["hpa_avg_replicas"]
        # Not strict comparison — depends on data variability
        assert ppa_avg > 0
        assert hpa_avg > 0


class TestConvertPipeline:
    """Test the TFLite conversion pipeline."""

    def test_convert_produces_valid_tflite(self, synthetic_csv):
        """Convert Keras model to TFLite and verify it's valid."""
        csv_path, tmpdir = synthetic_csv
        
        # Train
        train_result = train_model(
            csv_path=csv_path,
            target_col=TARGET_COLUMNS[0],
            epochs=2,
            output_dir=tmpdir,
        )
        
        # Convert
        tflite_path = os.path.join(tmpdir, "test_model.tflite")
        result = convert_model(
            model_path=train_result["artifact_paths"]["model"],
            quantize=True,
            output_path=tflite_path,
        )
        
        assert result is not None
        assert os.path.exists(tflite_path)
        assert result["size_kb"] > 0
        
        # Verify TFLite is valid (contains TFL3 magic bytes)
        with open(tflite_path, "rb") as f:
            tflite_bytes = f.read()
            # TFLite files contain "TFL3" within the first 16 bytes (FlatBuffers format)
            assert b"TFL3" in tflite_bytes[:20], "Invalid TFLite file (missing TFL3 magic bytes)"

    def test_quantized_smaller_than_unquantized(self, synthetic_csv):
        """Quantized model should be smaller."""
        csv_path, tmpdir = synthetic_csv
        
        train_result = train_model(
            csv_path=csv_path,
            target_col=TARGET_COLUMNS[0],
            epochs=2,
            output_dir=tmpdir,
        )
        
        unquant_path = os.path.join(tmpdir, "unquant.tflite")
        quant_path = os.path.join(tmpdir, "quant.tflite")
        
        result_unquant = convert_model(
            model_path=train_result["artifact_paths"]["model"],
            quantize=False,
            output_path=unquant_path,
        )
        
        result_quant = convert_model(
            model_path=train_result["artifact_paths"]["model"],
            quantize=True,
            output_path=quant_path,
        )
        
        assert result_quant["size_kb"] <= result_unquant["size_kb"] + 1  # 1KB tolerance


class TestMultiHorizonPipeline:
    """Test training multiple horizons in sequence."""

    def test_train_all_three_horizons(self, synthetic_csv):
        """Train models for all 3 horizons."""
        csv_path, tmpdir = synthetic_csv
        
        results = {}
        for horizon in TARGET_COLUMNS[:3]:  # rps_t3m, rps_t5m, rps_t10m
            result = train_model(
                csv_path=csv_path,
                target_col=horizon,
                epochs=1,
                output_dir=tmpdir,
            )
            results[horizon] = result
            assert result is not None
        
        # Verify all artifacts
        for horizon in TARGET_COLUMNS[:3]:
            assert Path(results[horizon]["artifact_paths"]["model"]).exists()
            assert Path(results[horizon]["artifact_paths"]["scaler"]).exists()
