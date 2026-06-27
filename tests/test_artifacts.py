from pathlib import Path

from ppa.model.artifacts import (
    artifact_dir,
    champion_dir,
    keras_model_path,
    tflite_model_path,
)


def test_structured_artifact_paths():
    root = Path("/tmp/artifacts")
    assert (
        artifact_dir("app", "ns", "rps_t10m", root) == root / "app" / "ns" / "rps_t10m"
    )
    assert (
        champion_dir("app", "ns", "rps_t10m", root) == root / "app" / "ns" / "rps_t10m"
    )
    # Canonical paths: no horizon suffix in filenames
    assert keras_model_path("app", "ns", "rps_t10m", root).name == "ppa_model.keras"
    assert tflite_model_path("app", "ns", "rps_t10m", root).name == "ppa_model.tflite"
