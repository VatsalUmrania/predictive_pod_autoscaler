from __future__ import annotations

import sys
import types
from pathlib import Path

from ppa.cli.commands import deploy_stages


class _DummyProgress:
    def update(self, *args, **kwargs):
        return None

    def advance(self, *args, **kwargs):
        return None


def test_retrain_lstm_uses_horizon_specific_artifacts(monkeypatch, tmp_path):
    csv_path = tmp_path / "training.csv"
    csv_path.write_text("timestamp,value\n2024-01-01T00:00:00Z,1\n")

    artifacts_dir = tmp_path / "artifacts"
    champion_dir = tmp_path / "champions"

    monkeypatch.setattr(deploy_stages, "CHAMPION_DIR", champion_dir)
    monkeypatch.setattr(deploy_stages, "step_heading", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_stages, "info", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_stages, "warn", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_stages, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_stages, "success", lambda *args, **kwargs: None)

    # Expected canonical paths (no horizon suffix in filenames)
    expected_model = artifacts_dir / "test-app" / "ns" / "rps_t10m" / "ppa_model.keras"
    expected_tflite = artifacts_dir / "test-app" / "ns" / "rps_t10m" / "ppa_model.tflite"
    artifacts_dir / "test-app" / "ns" / "rps_t10m" / "scaler.pkl"
    artifacts_dir / "test-app" / "ns" / "rps_t10m" / "target_scaler.pkl"

    def fake_train_model(**kwargs):
        out_dir = Path(kwargs["output_dir"])
        out_dir = out_dir / "test-app" / "ns" / "rps_t10m"
        out_dir.mkdir(parents=True, exist_ok=True)
        for path in [
            out_dir / "ppa_model.keras",
            out_dir / "scaler.pkl",
            out_dir / "target_scaler.pkl",
            out_dir / "split_meta.json",
        ]:
            path.write_text("artifact")
        return {
            "metrics": {"val_mae": 0.1234},
            "artifact_paths": {
                "model": str(out_dir / "ppa_model.keras"),
                "scaler": str(out_dir / "scaler.pkl"),
                "target_scaler": str(out_dir / "target_scaler.pkl"),
                "meta": str(out_dir / "split_meta.json"),
            },
        }

    def fake_convert_model(*, model_path, output_path, quantize):
        assert Path(model_path) == expected_model
        assert Path(output_path) == expected_tflite
        assert quantize is True
        expected_tflite.write_text("tflite")
        expected_tflite.with_name("ppa_model_metadata.json").write_text("metadata")
        return {"output_path": str(expected_tflite), "size_kb": 1.0}

    fake_model_pkg = types.ModuleType("ppa.model")
    fake_model_pkg.__path__ = []
    fake_train_mod = types.ModuleType("ppa.model.train")
    fake_train_mod.train_model = fake_train_model
    fake_convert_mod = types.ModuleType("ppa.model.convert")
    fake_convert_mod.convert_model = fake_convert_model

    monkeypatch.setitem(sys.modules, "ppa.model", fake_model_pkg)
    monkeypatch.setitem(sys.modules, "ppa.model.train", fake_train_mod)
    monkeypatch.setitem(sys.modules, "ppa.model.convert", fake_convert_mod)

    current = deploy_stages.retrain_lstm(
        progress=_DummyProgress(),
        task=1,
        current=0,
        total_steps=3,
        app_name="test-app",
        namespace="ns",
        csv=str(csv_path),
        horizon="rps_t10m",
        lookback=60,
        epochs=1,
        patience=1,
        artifacts_dir=str(artifacts_dir),
    )

    assert current == 3
    # Verify canonical paths were promoted (no horizon suffix in filenames)
    assert (champion_dir / "test-app" / "ns" / "rps_t10m" / "ppa_model.keras").exists()
    assert (champion_dir / "test-app" / "ns" / "rps_t10m" / "ppa_model.tflite").exists()
    assert (champion_dir / "test-app" / "ns" / "rps_t10m" / "scaler.pkl").exists()
    assert (champion_dir / "test-app" / "ns" / "rps_t10m" / "target_scaler.pkl").exists()
