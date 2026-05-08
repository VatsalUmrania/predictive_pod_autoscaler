"""Versioned model bundle resolution and validation for the operator."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from ppa.config import LOOKBACK_STEPS

logger = logging.getLogger("ppa.model_bundle")

MAX_LOAD_RETRIES = 5
LOAD_RETRY_INITIAL_DELAY_SECONDS = 0.2
LOAD_RETRY_MAX_DELAY_SECONDS = 2.0
LOAD_RETRY_JITTER = 0.2


class BundlePendingError(RuntimeError):
    """Raised when the current pointer is temporarily unreadable."""


class BundleValidationError(RuntimeError):
    """Raised when a bundle is stable but invalid."""


@dataclass(frozen=True)
class ModelBundle:
    root: Path
    model_path: Path
    scaler_path: Path
    target_scaler_path: Path | None
    metadata_path: Path | None
    version: str
    metadata: dict[str, Any]
    legacy: bool = False


def _jittered_delay(attempt: int) -> float:
    base = min(
        LOAD_RETRY_MAX_DELAY_SECONDS,
        LOAD_RETRY_INITIAL_DELAY_SECONDS * (2**attempt),
    )
    return base * random.uniform(1.0 - LOAD_RETRY_JITTER, 1.0 + LOAD_RETRY_JITTER)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_metadata(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BundleValidationError(f"metadata corrupted: {path}: {exc}") from exc
    except OSError as exc:
        raise BundlePendingError(f"metadata temporarily unreadable: {path}: {exc}") from exc


def _find_metadata(base: Path, model_path: Path, canonical: bool) -> Path | None:
    candidates = [base / "metadata.json"]
    if not canonical:
        candidates.extend(
            [
                base / "ppa_model_metadata.json",
                base / f"{model_path.stem}_metadata.json",
            ]
        )
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _verify_checksums(bundle: ModelBundle) -> None:
    checksums = bundle.metadata.get("checksums") or {}
    for key, path in {
        "model": bundle.model_path,
        "scaler": bundle.scaler_path,
        "target_scaler": bundle.target_scaler_path,
    }.items():
        expected = checksums.get(key)
        if expected and path is not None and _sha256(path) != expected:
            raise BundleValidationError(f"{key} checksum mismatch for {path}")


def _validate_metadata(bundle: ModelBundle, app: str, horizon: str) -> None:
    if bundle.legacy:
        # Legacy bundles may not carry full metadata. Predictor-level validation still applies.
        return

    metadata = bundle.metadata
    missing = [
        field
        for field in ("app", "horizon", "version", "feature_columns", "lookback", "input_shape")
        if field not in metadata
    ]
    if missing:
        raise BundleValidationError(f"metadata missing required fields: {', '.join(missing)}")
    if metadata["app"] != app:
        raise BundleValidationError(f"metadata app mismatch: {metadata['app']} != {app}")
    if metadata["horizon"] != horizon:
        raise BundleValidationError(f"metadata horizon mismatch: {metadata['horizon']} != {horizon}")
    if metadata["feature_columns"] != FEATURE_COLUMNS:
        raise BundleValidationError("metadata feature_columns mismatch")
    if int(metadata["lookback"]) != LOOKBACK_STEPS:
        raise BundleValidationError(f"metadata lookback mismatch: {metadata['lookback']}")

    expected_shapes = [
        [1, LOOKBACK_STEPS, NUM_FEATURES],
        [-1, LOOKBACK_STEPS, NUM_FEATURES],
    ]
    if list(metadata["input_shape"]) not in expected_shapes:
        raise BundleValidationError(
            f"metadata input_shape mismatch: {metadata['input_shape']}"
        )


def _build_bundle(base: Path, app: str, horizon: str, legacy: bool) -> ModelBundle:
    canonical_model = base / "model.tflite"
    legacy_model = base / "ppa_model.tflite"
    model_path = canonical_model if canonical_model.exists() else legacy_model
    scaler_path = base / "scaler.pkl"
    target_scaler_path = base / "target_scaler.pkl"
    metadata_path = _find_metadata(base, model_path, canonical=canonical_model.exists())

    missing = [path for path in (model_path, scaler_path) if not path.exists()]
    if missing:
        raise BundlePendingError(f"bundle files not visible yet: {missing}")

    if not legacy and not (base / "READY").exists():
        raise BundlePendingError(f"bundle READY marker missing: {base}")

    metadata = _read_metadata(metadata_path)
    version = str(metadata.get("version") or base.resolve().name)
    bundle = ModelBundle(
        root=base,
        model_path=model_path,
        scaler_path=scaler_path,
        target_scaler_path=target_scaler_path if target_scaler_path.exists() else None,
        metadata_path=metadata_path,
        version=version,
        metadata=metadata,
        legacy=legacy,
    )
    _validate_metadata(bundle, app, horizon)
    _verify_checksums(bundle)
    return bundle


def resolve_model_bundle_once(model_dir: str, app: str, horizon: str) -> ModelBundle:
    root = Path(model_dir)
    structured = root / app / horizon / "current"
    if structured.exists():
        return _build_bundle(structured, app, horizon, legacy=False)

    flat = root / horizon
    if flat.exists():
        logger.warning("Using legacy flat model bundle path: %s", flat)
        return _build_bundle(flat, app, horizon, legacy=True)

    raise BundlePendingError(f"no model bundle found for {app}/{horizon}")


def resolve_model_bundle(model_dir: str, app: str, horizon: str) -> ModelBundle:
    last_pending: BundlePendingError | None = None
    for attempt in range(MAX_LOAD_RETRIES):
        try:
            return resolve_model_bundle_once(model_dir, app, horizon)
        except BundlePendingError as exc:
            last_pending = exc
            if attempt == MAX_LOAD_RETRIES - 1:
                break
            time.sleep(_jittered_delay(attempt))
    assert last_pending is not None
    raise last_pending


def validate_prediction_sample(predictions: list[float], min_allowed: float, max_allowed: float) -> None:
    if not predictions:
        raise BundleValidationError("semantic validation produced no predictions")
    for pred in predictions:
        if not math.isfinite(pred):
            raise BundleValidationError("semantic validation produced NaN/inf")
        if pred < min_allowed or pred > max_allowed:
            raise BundleValidationError(
                f"semantic prediction out of bounds: {pred} not in [{min_allowed}, {max_allowed}]"
            )
    if len(predictions) >= 3 and max(predictions) - min(predictions) < 1e-6:
        raise BundleValidationError("semantic validation detected constant model output")


__all__ = [
    "BundlePendingError",
    "BundleValidationError",
    "MAX_LOAD_RETRIES",
    "ModelBundle",
    "resolve_model_bundle",
    "resolve_model_bundle_once",
    "validate_prediction_sample",
]
