# operator/predictor.py — TFLite inference wrapper
"""Wrap the LSTM model for online prediction with rolling history."""

import logging
import sys
import time
import json
from collections import deque
from pathlib import Path

import joblib
import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from config import LOOKBACK_STEPS, TIMER_INTERVAL

logger = logging.getLogger("ppa.predictor")


class Predictor:
    """Per-CR predictor: loads model + scaler from given paths."""

    def __init__(self, model_path: str, scaler_path: str, target_scaler_path: str | None = None):
        self.model_path = model_path
        self.scaler_path = scaler_path
        self.target_scaler_path = target_scaler_path
        self.history: deque = deque(maxlen=LOOKBACK_STEPS)
        self.interpreter = None
        self.scaler = None
        self.target_scaler = None
        self.input_details = None
        self.output_details = None
        self._load_failed = False
        # FIX (PR#10): Add exponential backoff for model reload failures
        self._load_failures = 0
        self._last_load_attempt = 0.0

        # FIX (PR#12): Concept drift detection — track prediction accuracy over time
        self.prediction_history: deque = deque(maxlen=60)  # Last 60 predictions (30 min at 30s interval)
        self.actual_history: deque = deque(maxlen=60)      # Last 60 actual values
        self.concept_drift_detected = False
        self.last_drift_check_time = 0.0

        self._try_load()

    def _load_and_validate_metadata(self):
        """FIX (PR#7): Load and validate model metadata to prevent schema mismatches."""
        model_dir = Path(self.model_path).parent
        metadata_path = model_dir / f"{Path(self.model_path).stem}_metadata.json"

        try:
            if metadata_path.exists():
                with open(metadata_path, 'r') as f:
                    metadata = json.load(f)

                # Validate critical schema fields
                if "feature_columns" in metadata:
                    if metadata["feature_columns"] != FEATURE_COLUMNS:
                        raise ValueError(
                            f"Feature column mismatch: model expects {metadata['feature_columns']}, "
                            f"but operator has {FEATURE_COLUMNS}"
                        )

                if "lookback" in metadata:
                    if metadata["lookback"] != LOOKBACK_STEPS:
                        logger.warning(
                            f"Lookback mismatch: model expects {metadata['lookback']}, "
                            f"but operator configured for {LOOKBACK_STEPS}. Using model's value."
                        )

                if "accuracy_loss_pct" in metadata and metadata["accuracy_loss_pct"] is not None:
                    if metadata["accuracy_loss_pct"] > 5.0:
                        logger.warning(
                            f"Model has high quantization loss: {metadata['accuracy_loss_pct']:.2f}% "
                            f"(threshold: 5.0%)"
                        )

                logger.info(f"Metadata validated: {metadata_path}")
                return metadata
            else:
                logger.warning(f"No metadata file found at {metadata_path}. "
                              f"Proceeding without schema validation (consider retraining)")
                return None
        except Exception as e:
            logger.warning(f"Failed to load/validate metadata: {e}")
            return None

    def _try_load(self):
        """Attempt to load model, scaler, and target scaler. Idempotent with exponential backoff."""
        if self.interpreter is not None and self.scaler is not None:
            return  # already loaded

        # FIX (PR#10): Implement exponential backoff to prevent disk thrashing
        if self._load_failed:
            elapsed = time.time() - self._last_load_attempt
            backoff = min(300, 2 ** min(self._load_failures, 10))  # Cap at 5 min, 2^10 = 1024
            if elapsed < backoff:
                return  # Don't retry yet, still in backoff period

        self._last_load_attempt = time.time()
        try:
            # FIX (PR#7): Load and validate metadata before loading model
            self._load_and_validate_metadata()

            # Try lightweight ai-edge-litert first, then tensorflow.lite, then tflite_runtime
            interpreter_loaded = False
            for loader_name, loader_fn in [
                ("ai_edge_litert", lambda: __import__("ai_edge_litert.interpreter", fromlist=["Interpreter"]).Interpreter),
                ("tensorflow.lite", lambda: __import__("tensorflow").lite.Interpreter),
                ("tflite_runtime", lambda: __import__("tflite_runtime.interpreter", fromlist=["Interpreter"]).Interpreter),
            ]:
                try:
                    InterpreterClass = loader_fn()
                    self.interpreter = InterpreterClass(model_path=self.model_path)
                    self.interpreter.allocate_tensors()
                    logger.info(f"Model loaded via {loader_name}")
                    interpreter_loaded = True
                    break
                except Exception as exc:
                    logger.debug(f"{loader_name} failed: {exc}")
                    continue

            if not interpreter_loaded:
                raise RuntimeError("No TFLite runtime found (tried ai_edge_litert, tensorflow.lite, tflite_runtime)")

            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()

            # Detect lookback from input tensor shape: (batch, lookback, features)
            input_shape = self.input_details[0]["shape"]
            self.lookback = input_shape[1]
            logger.info(f"Detected model lookback: {self.lookback}")

            # Re-initialize history with the detected lookback
            self.history = deque(maxlen=self.lookback)

            self.scaler = joblib.load(self.scaler_path)

            # Target scaler: inverse-transforms model output [0,1] → raw RPS
            self.target_scaler = None
            if self.target_scaler_path:
                self.target_scaler = joblib.load(self.target_scaler_path)
                logger.info(f"Loaded target scaler from {self.target_scaler_path}")

            logger.info(f"Loaded model from {self.model_path}, scaler from {self.scaler_path}")
            self._load_failed = False
            self._load_failures = 0  # Reset on success
        except Exception as exc:
            logger.error(f"Failed to load model/scaler: {exc}")
            self.interpreter = None
            self.scaler = None
            self.target_scaler = None
            self._load_failed = True
            self._load_failures += 1
            if self._load_failures > 10:
                logger.critical(f"Model failed to load {self._load_failures} times, giving up")

    def paths_match(self, model_path: str, scaler_path: str, target_scaler_path: str | None = None) -> bool:
        return (
            self.model_path == model_path
            and self.scaler_path == scaler_path
            and self.target_scaler_path == target_scaler_path
        )

    def copy_history(self) -> list:
        """Return a copy of current history for preservation during model upgrade (PR#5)."""
        return list(self.history)

    def restore_history(self, history_snapshot: list) -> None:
        """Restore history from a snapshot (used during model upgrade to avoid losing state)."""
        self.history.clear()
        for row in history_snapshot:
            self.history.append(row)
        logger.info(f"Restored history: {len(self.history)}/{self.history.maxlen} steps")

    def update(self, features: dict):
        row = np.array([features[name] for name in FEATURE_COLUMNS], dtype=np.float32)
        self.history.append(row)

    def ready(self) -> bool:
        # Retry loading if previous attempt failed (e.g. missing dependency installed later)
        if self._load_failed:
            self._try_load()
        return (
            len(self.history) >= self.lookback
            and self.interpreter is not None
            and self.scaler is not None
        )

    def predict(self) -> float:
        if not self.ready():
            return 0.0

        window = np.array(self.history, dtype=np.float32)[-self.lookback:]
        scaled = self.scaler.transform(window)
        input_data = scaled.reshape(1, self.lookback, NUM_FEATURES).astype(np.float32)

        # FIX (PR#13): Track inference latency
        start_time = time.time()
        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()
        inference_time = (time.time() - start_time) * 1000  # Convert to milliseconds

        if inference_time > 100:  # >100ms is concerning
            logger.warning(f"Slow inference: {inference_time:.1f}ms (expected <100ms)")

        output = self.interpreter.get_tensor(self.output_details[0]["index"])

        predicted_scaled = float(output[0][0])

        # Inverse-transform model output using target scaler
        if self.target_scaler is not None:
            predicted_rps = self.target_scaler.inverse_transform(
                np.array([[predicted_scaled]])
            )[0, 0]
        else:
            # Legacy fallback: model was trained without target scaling
            predicted_rps = predicted_scaled

        return max(0.0, float(predicted_rps))

    def prefill_from_history(self, feature_rows: list[dict]):
        """Populate history deque from a list of feature dictionaries (e.g. from Prometheus range query)."""
        for features in feature_rows:
            row = np.array([features.get(name, 0.0) for name in FEATURE_COLUMNS], dtype=np.float32)
            self.history.append(row)
        filled = len(self.history)
        logger.info(f"Prefilled history: {filled}/{self.history.maxlen} steps loaded from startup fetch")

    def track_prediction_accuracy(self, predicted_rps: float, actual_rps: float):
        """
        FIX (PR#12): Track prediction vs actual RPS for concept drift detection.
        """
        self.prediction_history.append(predicted_rps)
        self.actual_history.append(actual_rps)

    def check_concept_drift(self) -> dict:
        """
        FIX (PR#12): Detect concept drift by comparing predicted vs actual RPS over last window.
        Returns: dict with keys 'detected', 'error_pct', 'drift_severity'
        """
        current_time = time.time()

        # Only check every 5 minutes (300 seconds) to avoid log spam
        if current_time - self.last_drift_check_time < 300:
            return {'detected': self.concept_drift_detected, 'checked': False}

        self.last_drift_check_time = current_time

        if len(self.prediction_history) < 10 or len(self.actual_history) < 10:
            return {'detected': False, 'checked': True, 'reason': 'insufficient_history'}

        # Compare predictions from 1 minute ago vs actual now
        # Use a sliding window approach
        recent_predictions = list(self.prediction_history)[-10:]  # Last 10 (5 minutes at 30s interval)
        recent_actuals = list(self.actual_history)[-10:]

        if not recent_predictions or not recent_actuals:
            return {'detected': False, 'checked': True}

        # Calculate MAPE (Mean Absolute Percentage Error)
        errors = []
        for pred, actual in zip(recent_predictions, recent_actuals):
            if actual > 0:
                error = abs(pred - actual) / actual * 100
                errors.append(error)

        if not errors:
            return {'detected': False, 'checked': True}

        mean_error_pct = np.mean(errors)

        # Drift severity levels
        drift_detected = mean_error_pct > 20  # >20% error indicates possible drift
        severe_drift = mean_error_pct > 50    # >50% error is severe

        if drift_detected or severe_drift:
            logger.warning(
                f"Concept drift detected: {mean_error_pct:.1f}% error over last 5 minutes "
                f"{'(SEVERE)' if severe_drift else ''}"
            )
            self.concept_drift_detected = True
        else:
            if self.concept_drift_detected:
                logger.info(f"Concept drift cleared: {mean_error_pct:.1f}% error")
            self.concept_drift_detected = False

        return {
            'detected': drift_detected,
            'error_pct': mean_error_pct,
            'severity': 'severe' if severe_drift else ('moderate' if drift_detected else 'normal'),
            'checked': True
        }

