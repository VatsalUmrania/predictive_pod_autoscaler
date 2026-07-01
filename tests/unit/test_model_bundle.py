from __future__ import annotations

import hashlib
import json

import pytest

from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from ppa.config import LOOKBACK_STEPS
from ppa.operator.model_bundle import (
    BundlePendingError,
    BundleValidationError,
    resolve_model_bundle_once,
)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bundle(root, *, app="test-app", horizon="rps_t10m", version="v1"):
    bundle = root / app / horizon / version
    bundle.mkdir(parents=True)
    (bundle / "model.tflite").write_bytes(b"TFL3-model")
    (bundle / "scaler.pkl").write_bytes(b"scaler")
    (bundle / "target_scaler.pkl").write_bytes(b"target")
    metadata = {
        "app": app,
        "horizon": horizon,
        "version": version,
        "created_at": "2026-04-25T00:00:00Z",
        "feature_columns": FEATURE_COLUMNS,
        "lookback": LOOKBACK_STEPS,
        "input_shape": [1, LOOKBACK_STEPS, NUM_FEATURES],
        "checksums": {
            "model": _sha(bundle / "model.tflite"),
            "scaler": _sha(bundle / "scaler.pkl"),
            "target_scaler": _sha(bundle / "target_scaler.pkl"),
        },
    }
    (bundle / "metadata.json").write_text(json.dumps(metadata))
    (bundle / "READY").write_text("ok\n")
    current = root / app / horizon / "current"
    current.symlink_to(bundle, target_is_directory=True)
    return bundle


def test_resolves_ready_versioned_bundle(tmp_path):
    _write_bundle(tmp_path)

    bundle = resolve_model_bundle_once(str(tmp_path), "test-app", "rps_t10m")

    assert bundle.version == "v1"
    assert bundle.model_path.name == "model.tflite"
    assert bundle.legacy is False


def test_rejects_missing_ready_marker(tmp_path):
    bundle_dir = _write_bundle(tmp_path)
    (bundle_dir / "READY").unlink()

    with pytest.raises(BundlePendingError, match="READY"):
        resolve_model_bundle_once(str(tmp_path), "test-app", "rps_t10m")


def test_rejects_horizon_mismatch(tmp_path):
    _write_bundle(tmp_path, horizon="rps_t10m")
    metadata_path = tmp_path / "test-app" / "rps_t10m" / "v1" / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["horizon"] = "rps_t3m"
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(BundleValidationError, match="horizon"):
        resolve_model_bundle_once(str(tmp_path), "test-app", "rps_t10m")
