# tests/test_pipeline_integration.py — Integration tests for full pipeline orchestrator
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS


def _make_synthetic_training_csv(tmpdir, n_rows=500):
    """Create a synthetic training CSV."""
    import numpy as np
    
    rng = np.random.default_rng(42)
    data = {col: rng.random(n_rows) * 100 for col in FEATURE_COLUMNS}
    for col in TARGET_COLUMNS:
        data[col] = rng.random(n_rows) * 50 + 20
    
    data["segment_id"] = np.zeros(n_rows, dtype=int)
    data["timestamp"] = pd.date_range("2026-03-01", periods=n_rows, freq="30s")
    
    df = pd.DataFrame(data).set_index("timestamp")
    csv_path = os.path.join(tmpdir, "training_data.csv")
    df.to_csv(csv_path)
    return csv_path


class TestPipelineOrchestrator:
    """Test the pipeline.py orchestrator script."""

    def test_pipeline_single_horizon(self):
        """Run pipeline for a single horizon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = _make_synthetic_training_csv(tmpdir)
            
            result = subprocess.run(
                [
                    sys.executable, "model/pipeline.py",
                    "--csv", csv_path,
                    "--horizons", "rps_t3m",
                    "--epochs", "2",
                    "--output-dir", tmpdir,
                    "--quality-gate", "100.0",  # High threshold for synthetic data
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            
            print(result.stdout)
            if result.returncode != 0:
                print("STDERR:", result.stderr)
            
            assert result.returncode == 0, f"Pipeline failed: {result.stderr}"
            
            # Verify artifacts
            horizon = "rps_t3m"
            assert Path(f"{tmpdir}/ppa_model_{horizon}.keras").exists()
            assert Path(f"{tmpdir}/ppa_model_{horizon}.tflite").exists()
            assert Path(f"{tmpdir}/scaler_{horizon}.pkl").exists()
            assert Path(f"{tmpdir}/eval_summary_{horizon}.json").exists()

    def test_pipeline_multiple_horizons(self):
        """Run pipeline for all 3 horizons."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = _make_synthetic_training_csv(tmpdir)
            
            result = subprocess.run(
                [
                    sys.executable, "model/pipeline.py",
                    "--csv", csv_path,
                    "--horizons", "rps_t3m,rps_t5m,rps_t10m",
                    "--epochs", "2",
                    "--output-dir", tmpdir,
                    "--quality-gate", "100.0",  # High threshold for synthetic data
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            
            print(result.stdout)
            if result.returncode != 0:
                print("STDERR:", result.stderr)
            
            assert result.returncode == 0
            
            # Verify all horizons have artifacts
            for horizon in ["rps_t3m", "rps_t5m", "rps_t10m"]:
                assert Path(f"{tmpdir}/ppa_model_{horizon}.keras").exists()
                assert Path(f"{tmpdir}/ppa_model_{horizon}.tflite").exists()
                assert Path(f"{tmpdir}/scaler_{horizon}.pkl").exists()
                assert Path(f"{tmpdir}/eval_summary_{horizon}.json").exists()
                
                # Verify summary has required keys
                with open(f"{tmpdir}/eval_summary_{horizon}.json") as f:
                    summary = json.load(f)
                    assert "mape" in summary
                    assert "mae" in summary
                    assert "rmse" in summary
                    assert "ppa_avg_replicas" in summary
                    assert "hpa_avg_replicas" in summary

    def test_pipeline_output_summary_table(self):
        """Verify pipeline produces a summary table in output."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = _make_synthetic_training_csv(tmpdir)
            
            result = subprocess.run(
                [
                    sys.executable, "model/pipeline.py",
                    "--csv", csv_path,
                    "--horizons", "rps_t3m,rps_t5m",
                    "--epochs", "1",
                    "--output-dir", tmpdir,
                    "--quality-gate", "100.0",  # High threshold for synthetic data
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            
            # Should contain "PIPELINE SUMMARY" in output
            assert "PIPELINE SUMMARY" in result.stdout
            assert "rps_t3m" in result.stdout
            assert "rps_t5m" in result.stdout

    def test_pipeline_quality_gate_warning(self):
        """Verify quality gate triggers warning when MAPE exceeded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = _make_synthetic_training_csv(tmpdir)
            
            result = subprocess.run(
                [
                    sys.executable, "model/pipeline.py",
                    "--csv", csv_path,
                    "--horizons", "rps_t3m",
                    "--epochs", "1",
                    "--output-dir", tmpdir,
                    "--quality-gate", "1.0",  # Very strict threshold
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            
            # Pipeline should exit with code 1 for quality gate failure
            assert result.returncode == 1, "Pipeline should fail when quality gate is exceeded"
            assert "WARN" in result.stdout or "quality gate" in result.stdout.lower()

    def test_pipeline_with_real_data_if_available(self):
        """Test pipeline with real training data if available."""
        real_csv = Path("data-collection/training-data/training_data_v2.csv")
        
        if not real_csv.exists():
            pytest.skip("Real training data not available")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                [
                    sys.executable, "model/pipeline.py",
                    "--csv", str(real_csv),
                    "--horizons", "rps_t3m",
                    "--epochs", "5",
                    "--output-dir", tmpdir,
                    "--quality-gate", "25.0",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            
            print(result.stdout)
            if result.returncode != 0:
                print("STDERR:", result.stderr)
            
            # Should pass with real data
            assert result.returncode in [0, 1], f"Unexpected error: {result.stderr}"
            
            # Verify artifacts exist
            assert Path(f"{tmpdir}/ppa_model_rps_t3m.keras").exists()
            assert Path(f"{tmpdir}/eval_summary_rps_t3m.json").exists()
