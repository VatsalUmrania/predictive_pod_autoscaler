"""Helpers for structured model artifact paths."""

from __future__ import annotations

from pathlib import Path

from ppa.config import ARTIFACTS_DIR, CHAMPION_DIR


def artifact_dir(app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR) -> Path:
    """Return the structured artifact directory for an app/namespace/target."""
    return root / app_name / namespace / target


def champion_dir(app_name: str, namespace: str, target: str, root: Path = CHAMPION_DIR) -> Path:
    """Return the structured champion directory for an app/namespace/target."""
    return root / app_name / namespace / target


def keras_model_path(
    app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR
) -> Path:
    """Return canonical path to Keras model (no horizon suffix in filename)."""
    return artifact_dir(app_name, namespace, target, root) / "ppa_model.keras"


def tflite_model_path(
    app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR
) -> Path:
    """Return canonical path to TFLite model (no horizon suffix in filename)."""
    return artifact_dir(app_name, namespace, target, root) / "ppa_model.tflite"


def scaler_path(app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR) -> Path:
    """Return canonical path to scaler (no horizon suffix in filename)."""
    return artifact_dir(app_name, namespace, target, root) / "scaler.pkl"


def target_scaler_path(
    app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR
) -> Path:
    """Return canonical path to target scaler (no horizon suffix in filename)."""
    return artifact_dir(app_name, namespace, target, root) / "target_scaler.pkl"


def metadata_path(app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR) -> Path:
    """Return canonical path to model metadata (no horizon suffix in filename)."""
    return artifact_dir(app_name, namespace, target, root) / "ppa_model_metadata.json"
