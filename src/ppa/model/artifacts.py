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
    return artifact_dir(app_name, namespace, target, root) / f"ppa_model_{target}.keras"


def tflite_model_path(
    app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR
) -> Path:
    return artifact_dir(app_name, namespace, target, root) / f"ppa_model_{target}.tflite"


def scaler_path(app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR) -> Path:
    return artifact_dir(app_name, namespace, target, root) / f"scaler_{target}.pkl"


def target_scaler_path(
    app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR
) -> Path:
    return artifact_dir(app_name, namespace, target, root) / f"target_scaler_{target}.pkl"


def metadata_path(app_name: str, namespace: str, target: str, root: Path = ARTIFACTS_DIR) -> Path:
    return artifact_dir(app_name, namespace, target, root) / f"ppa_model_{target}_metadata.json"
