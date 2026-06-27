# operator/predictor.py — TFLite inference wrapper
"""Wrap the LSTM model for online prediction with rolling history."""

import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import joblib
import numpy as np

if TYPE_CHECKING:
    import ai_edge_litert as tflite
    from sklearn.preprocessing import StandardScaler

from ppa.common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from ppa.config import LOOKBACK_STEPS
from ppa.operator.diagnostics import (
    diagnose_model_load_issue,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

logger = logging.getLogger("ppa.predictor")

__all__ = ["Predictor"]


class Predictor:
    """Per-CR predictor: loads model + scaler from given paths."""

    def __init__(
        self,
        model_path: str,
        scaler_path: str,
        target_scaler_path: str | None = None,
        metadata_path: str | None = None,
        version: str | None = None,
    ):
        self.model_path = model_path
        self.scaler_path = scaler_path
        self.target_scaler_path = target_scaler_path
        self.metadata_path = metadata_path
        self.version = version
        self.history: deque = deque(maxlen=LOOKBACK_STEPS)
        self.interpreter: tflite.Interpreter | None = None
        self.scaler: StandardScaler | None = None
        self.target_scaler: StandardScaler | None = None
        self.input_details: list[dict[str, int]] | None = None
        self.output_details: list[dict[str, int]] | None = None
        self._load_failed = False
        self.last_load_error: str | None = None
        self.model_load_time_ms = 0.0
        self.last_prediction_latency_ms = 0.0
        # FIX (PR#10): Add exponential backoff for model reload failures
        self._load_failures = 0
        self._last_load_attempt = 0.0
        # FIX (PR#8): Initialize lookback to prevent AttributeError if load fails
        self.lookback = LOOKBACK_STEPS

        # FIX (PR#12): Concept drift detection — track prediction accuracy over time
        self.prediction_history: deque = deque(
            maxlen=60
        )  # Last 60 predictions (30 min at 30s interval)
        self.actual_history: deque = deque(maxlen=60)  # Last 60 actual values
        self.concept_drift_detected = False
        self.last_drift_check_time = 0.0

        self._try_load()

    def _load_and_validate_metadata(self):
        """FIX (PR#7/PR#8): Load and validate model metadata to prevent schema mismatches.

        CRITICAL ERRORS (re-raised immediately to fail fast):
        - Feature column mismatch: model trained with different features
        - JSON parse error: metadata corrupted

        WARNINGS (logged but don't fail):
        - Lookback mismatch: model expects different history length
        - High quantization loss: model has degraded inference accuracy
        - Missing metadata file: proceed without validation (backward compat)
        """
        model_dir = Path(self.model_path).parent
        metadata_candidates = []
        if self.metadata_path:
            metadata_candidates.append(Path(self.metadata_path))
        metadata_candidates.extend(
            [
                model_dir / "metadata.json",
                model_dir / "ppa_model_metadata.json",
                model_dir / f"{Path(self.model_path).stem}_metadata.json",
            ]
        )
        metadata_path = next(
            (path for path in metadata_candidates if path.exists()), None
        )

        if metadata_path is None:
            logger.warning(
                f"No metadata file found near {self.model_path}. "
                f"Proceeding without schema validation (consider retraining)"
            )
            return None

        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
        except json.JSONDecodeError as e:
            # FIX (PR#8): JSON corruption is critical — fail fast
            raise ValueError(f"Metadata file corrupted: {e}") from e
        except Exception as e:
            raise ValueError(f"Failed to read metadata file: {e}") from e

        # Validate critical schema fields — these are deal-breakers
        if "feature_columns" in metadata:
            if metadata["feature_columns"] != FEATURE_COLUMNS:
                # FIX (PR#8): Re-raise immediately instead of logging and continuing
                # This ensures schema mismatches are caught at load time, not after 30min
                raise ValueError(
                    f"Feature column mismatch: model expects {metadata['feature_columns']}, "
                    f"but operator has {FEATURE_COLUMNS}"
                )

        # Non-critical warnings — log and continue
        if "lookback" in metadata:
            if metadata["lookback"] != LOOKBACK_STEPS:
                logger.warning(
                    f"Lookback mismatch: model expects {metadata['lookback']}, "
                    f"but operator configured for {LOOKBACK_STEPS}. Using model's value."
                )

        if (
            "accuracy_loss_pct" in metadata
            and metadata["accuracy_loss_pct"] is not None
        ):
            if metadata["accuracy_loss_pct"] > 5.0:
                logger.warning(
                    f"Model has high quantization loss: {metadata['accuracy_loss_pct']:.2f}% "
                    f"(threshold: 5.0%)"
                )

        if "version" in metadata:
            self.version = str(metadata["version"])

        logger.info(f"Metadata validated: {metadata_path}")
        return metadata

    def _try_load(self):
        """Attempt to load model, scaler, and target scaler. Idempotent with exponential backoff."""
        if self.interpreter is not None and self.scaler is not None:
            return  # already loaded

        # FIX (PR#10): Implement exponential backoff to prevent disk thrashing
        if self._load_failed:
            elapsed = time.time() - self._last_load_attempt
            backoff = min(
                300, 2 ** min(self._load_failures, 10)
            )  # Cap at 5 min, 2^10 = 1024
            if elapsed < backoff:
                return  # Don't retry yet, still in backoff period

        self._last_load_attempt = time.time()
        load_started = time.time()

        # GRANULAR ERROR TRACKING: Track which step fails
        load_step = "initialization"
        try:
            load_step = "metadata_validation"
            # FIX (PR#7): Load and validate metadata before loading model
            self._load_and_validate_metadata()
            logger.info(f"✓ {load_step} succeeded")

            load_step = "interpreter_load"
            # Try lightweight LiteRT first, then tensorflow.lite, then tflite_runtime.
            interpreter_loaded = False
            loader_attempts = []
            for loader_name, loader_fn in [
                (
                    "ai_edge_litert",
                    lambda: _load_ai_edge_litert_interpreter(),
                ),
                ("tensorflow.lite", lambda: __import__("tensorflow").lite.Interpreter),
                (
                    "tflite_runtime",
                    lambda: __import__(
                        "tflite_runtime.interpreter", fromlist=["Interpreter"]
                    ).Interpreter,
                ),
            ]:
                try:
                    interpreter_class = loader_fn()
                    logger.debug(
                        f"Attempting to load model via {loader_name} with path: {self.model_path}"
                    )
                    self.interpreter = interpreter_class(model_path=self.model_path)
                    logger.debug(f"Interpreter instantiated via {loader_name}")
                    self.interpreter.allocate_tensors()
                    logger.info(f"✓ {load_step} succeeded via {loader_name}")
                    interpreter_loaded = True
                    break
                except Exception as exc:
                    error_msg = f"{loader_name}: {type(exc).__name__}: {str(exc)}"
                    loader_attempts.append(error_msg)
                    logger.debug(error_msg)
                    continue

            if not interpreter_loaded:
                detailed_errors = "\n  ".join(loader_attempts)
                # FIX (PR#20): Run comprehensive diagnostics on first load failure
                if self._load_failures == 1:
                    logger.warning(
                        "First load failure detected - running diagnostics..."
                    )
                    try:
                        diagnostic_report = diagnose_model_load_issue(
                            self.model_path, self.scaler_path, self.target_scaler_path
                        )
                        logger.error(f"Diagnostic Report: {diagnostic_report}")
                    except Exception as diag_exc:
                        logger.warning(f"Diagnostics failed: {diag_exc}")

                raise RuntimeError(
                    f"No TFLite runtime found. Attempted:\n  {detailed_errors}"
                )

            # Load scaler
            load_step = "scaler_load"
            try:
                logger.debug(f"Loading scaler from: {self.scaler_path}")
                self.scaler = joblib.load(self.scaler_path)
                logger.info(f"✓ {load_step} succeeded: {self.scaler_path}")
            except Exception as e:
                raise RuntimeError(f"Failed to load required scaler: {e}") from e

            # Load target scaler (optional)
            load_step = "target_scaler_load"
            if self.target_scaler_path:
                try:
                    logger.debug(
                        f"Loading target scaler from: {self.target_scaler_path}"
                    )
                    self.target_scaler = joblib.load(self.target_scaler_path)
                    logger.info(f"✓ {load_step} succeeded: {self.target_scaler_path}")
                except Exception as e:
                    logger.warning(f"Failed to load target scaler: {e}")
                    import traceback

                    logger.warning(f"Target scaler traceback: {traceback.format_exc()}")
                    self.target_scaler = None

            # Get input and output tensor details
            load_step = "tensor_details"
            logger.debug("Getting tensor details...")
            try:
                self.input_details = self.interpreter.get_input_details()
                logger.debug(
                    f"Input details retrieved: {len(self.input_details)} tensors"
                )
                self.output_details = self.interpreter.get_output_details()
                logger.debug(
                    f"Output details retrieved: {len(self.output_details)} tensors"
                )
                input_shape = list(self.input_details[0].get("shape", []))
                # Validate shape has exactly 3 dims: [batch, lookback, features]
                if len(input_shape) != 3 or input_shape[1] != LOOKBACK_STEPS:
                    raise RuntimeError(
                        f"Model input shape {input_shape} incompatible: "
                        f"expected [batch, {LOOKBACK_STEPS}, N_features]"
                    )
                # Store the model's actual feature count — may differ from the
                # current codebase's NUM_FEATURES when model was trained on an
                # older/newer schema. The scaler must be regenerated to match.
                self.model_num_features = int(input_shape[2])
                if self.model_num_features != NUM_FEATURES:
                    logger.warning(
                        f"Model has {self.model_num_features} features but codebase "
                        f"FEATURE_COLUMNS has {NUM_FEATURES}. "
                        f"Scaler must be fit on the same {self.model_num_features} features "
                        f"the model was trained on."
                    )
                logger.info(
                    f"✓ {load_step} succeeded "
                    f"(model features={self.model_num_features}, schema features={NUM_FEATURES})"
                )

                # Now that model_num_features is known, validate the scaler matches
                load_step = "scaler_feature_check"
                scaler_features = getattr(self.scaler, "n_features_in_", None)
                if (
                    scaler_features is not None
                    and scaler_features != self.model_num_features
                ):
                    raise RuntimeError(
                        f"Scaler/model feature mismatch: "
                        f"scaler was fit on {scaler_features} features but "
                        f"model expects {self.model_num_features}. "
                        f"Re-run 'ppa model push' to regenerate scalers for this model."
                    )
                logger.info(
                    f"✓ {load_step} succeeded (scaler_features={scaler_features})"
                )
            except Exception as e:
                logger.error(f"Failed to get tensor details: {e}")
                import traceback

                logger.error(f"Tensor details traceback: {traceback.format_exc()}")
                raise

            self._load_failed = False
            self._load_failures = 0
            self.last_load_error = None
            self.model_load_time_ms = (time.time() - load_started) * 1000
            logger.info("All components loaded successfully")

        except Exception as e:
            import traceback

            logger.error(f"Failed at step '{load_step}': {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            self._load_failed = True
            self._load_failures += 1
            self.last_load_error = str(e)
            self.interpreter = None
            self.scaler = None
            self.target_scaler = None
            self.input_details = None
            self.output_details = None

    def paths_match(
        self, model_path: str, scaler_path: str, target_scaler_path: str | None = None
    ) -> bool:
        """Check if model paths match current paths (used to detect model upgrades)."""
        return (
            self.model_path == model_path
            and self.scaler_path == scaler_path
            and self.target_scaler_path == target_scaler_path
        )

    def is_loaded(self) -> bool:
        return (
            not self._load_failed
            and self.interpreter is not None
            and self.scaler is not None
            and self.input_details is not None
            and self.output_details is not None
        )

    def copy_history(self) -> list:
        """Return a copy of current history for preservation during model upgrade (PR#5)."""
        return list(self.history)

    def restore_history(self, history_snapshot: list) -> None:
        """Restore history from a snapshot (used during model upgrade to avoid losing state)."""
        self.history.clear()
        for row in history_snapshot:
            self.history.append(row)
        logger.info(
            f"Restored history: {len(self.history)}/{self.history.maxlen} steps"
        )

    def update(self, features: dict):
        row = np.array([features[name] for name in FEATURE_COLUMNS], dtype=np.float32)
        self.history.append(row)

    def ready(self) -> bool:
        # Retry loading if previous attempt failed (e.g. missing dependency installed later)
        if self._load_failed:
            self._try_load()
        return len(self.history) >= self.lookback and self.is_loaded()

    def predict(self, rolling_max_rps: float | None = None) -> float:
        """Run inference and return predicted load in req/s.

        The foundation model (alibaba_foundation_model.ipynb) outputs a
        normalized ratio in [0, 1] — ``target_normalized_rps_t3m``.
        To recover actual req/s the ratio is multiplied by the caller-supplied
        ``rolling_max_rps`` (the same denominator used during training).

        ``target_scaler`` is intentionally bypassed: the Colab training pipeline
        never created a target scaler.  Any ``target_scaler.pkl`` on disk was
        produced by ``regenerate_scalers.py`` fitting on a raw-RPS column
        (data_max ≈ 345), which turns a correct 0.53 ratio into a phantom
        345 req/s prediction.  See GitHub issue: scaling hallucination.
        """
        if not self.ready():
            return 0.0

        # Type narrowing: guarantee non-None after ready() check
        assert self.scaler is not None, "Scaler must be loaded after ready() check"
        assert (
            self.interpreter is not None
        ), "Interpreter must be loaded after ready() check"
        assert (
            self.input_details is not None and self.output_details is not None
        ), "Input/output details must be available after ready() check"

        window = np.array(self.history, dtype=np.float32)[-self.lookback :]
        # Use model's actual feature count (may differ from schema NUM_FEATURES)
        model_features = getattr(self, "model_num_features", NUM_FEATURES)
        history_features = window.shape[1]

        if history_features != model_features:
            raise RuntimeError(
                f"Feature count mismatch at inference: "
                f"history has {history_features} features but model expects {model_features}. "
                f"Ensure operator FEATURE_COLUMNS matches the model's training schema "
                f"and re-run 'ppa model push' to regenerate scalers."
            )

        scaled = self.scaler.transform(window)
        input_data = scaled.reshape(1, self.lookback, model_features).astype(np.float32)
        # FIX (PR#13): Track inference latency
        start_time = time.time()
        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()
        inference_time = (time.time() - start_time) * 1000  # Convert to milliseconds
        self.last_prediction_latency_ms = inference_time

        if inference_time > 100:  # >100ms is concerning
            logger.warning(f"Slow inference: {inference_time:.1f}ms (expected <100ms)")

        output = self.interpreter.get_tensor(self.output_details[0]["index"])

        predicted_scaled = float(output[0][0])

        # --- Denormalization ---
        # The foundation model (Colab) was trained on target_normalized_rps_t3m
        # which is a [0, 1] ratio (= future_rps / rolling_max_rps).  It outputs
        # a normalized ratio, NOT raw req/s.
        #
        # Correct denormalization: predicted_rps = ratio × rolling_max_rps
        # WRONG denormalization: target_scaler.inverse_transform(ratio)
        #   — the target_scaler on disk was fit on a raw-RPS column (max≈345)
        #     so it maps a valid 0.53 ratio → 344 req/s (hallucination).
        #
        # We bypass target_scaler entirely and use the caller-supplied
        # rolling_max_rps (floor 1.0, matching training) for denormalization.
        if rolling_max_rps is not None and rolling_max_rps > 0:
            predicted_rps = predicted_scaled * rolling_max_rps
            logger.debug(
                f"[predict] ratio={predicted_scaled:.4f}  "
                f"rolling_max_rps={rolling_max_rps:.2f}  "
                f"→ predicted_rps={predicted_rps:.2f}"
            )
        elif self.target_scaler is not None:
            # Fallback for old-style models that genuinely used a target scaler
            # fit on the correct target distribution.  Log a warning because this
            # path should not be reached for the foundation model.
            result = self.target_scaler.inverse_transform(
                np.array([[predicted_scaled]])
            )
            predicted_rps = result[0, 0]
            logger.warning(
                f"[predict] Using target_scaler fallback (no rolling_max_rps supplied). "
                f"raw={predicted_scaled:.4f} → {predicted_rps:.4f}. "
                f"target_scaler data_max={getattr(self.target_scaler, 'data_max_', '?')}"
            )
        else:
            # No denormalization available — return ratio as-is (legacy path).
            predicted_rps = predicted_scaled
            logger.debug(
                f"[predict] No denormalization (no rolling_max_rps, no target_scaler). "
                f"Returning raw model output={predicted_scaled:.4f}"
            )

        return max(0.0, float(predicted_rps))

    def serialize_history(self) -> list[list[float]] | None:
        """
        FIX (PR#15): Serialize history for storage in CR status.
        Returns compact list format suitable for JSON serialization.
        """
        if not self.history:
            return None
        # Convert deque to list of lists (JSON serializable)
        return [row.tolist() for row in self.history]

    def deserialize_history(self, serialized: list[list[float]]) -> bool:
        """
        FIX (PR#15): Restore history from serialized format.
        Returns True if successfully restored.
        """
        try:
            self.history.clear()
            for row in serialized:
                self.history.append(np.array(row, dtype=np.float32))
            logger.info(
                f"Deserialized history: {len(self.history)}/{self.history.maxlen} steps"
            )
            return True
        except Exception as exc:
            logger.warning(f"Failed to deserialize history: {exc}")
            return False

    def get_history_summary(self) -> dict:
        """
        Get a summary of history state for CR status.
        Compact representation for monitoring.
        """
        return {
            "filled_steps": len(self.history),
            "max_steps": self.history.maxlen,
            "ready": self.ready(),
            "last_drift_check": (
                datetime.fromtimestamp(
                    self.last_drift_check_time, tz=timezone.utc
                ).isoformat()
                if self.last_drift_check_time > 0
                else None
            ),
            "drift_detected": self.concept_drift_detected,
        }

    def prefill_history(self, feature_rows: list) -> None:
        """Populate history deque from a list of feature dictionaries (e.g. from Prometheus range query)."""
        for features in feature_rows:
            row = np.array(
                [features.get(name, 0.0) for name in FEATURE_COLUMNS], dtype=np.float32
            )
            self.history.append(row)
        filled = len(self.history)
        logger.info(
            f"Prefilled history: {filled}/{self.history.maxlen} steps loaded from startup fetch"
        )

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
            return {"detected": self.concept_drift_detected, "checked": False}

        self.last_drift_check_time = current_time

        if len(self.prediction_history) < 10 or len(self.actual_history) < 10:
            return {
                "detected": False,
                "checked": True,
                "reason": "insufficient_history",
            }

        # Compare predictions from 1 minute ago vs actual now
        # Use a sliding window approach
        recent_predictions = list(self.prediction_history)[
            -10:
        ]  # Last 10 (5 minutes at 30s interval)
        recent_actuals = list(self.actual_history)[-10:]

        if not recent_predictions or not recent_actuals:
            return {"detected": False, "checked": True}

        # Calculate MAPE (Mean Absolute Percentage Error)
        errors = []
        for pred, actual in zip(recent_predictions, recent_actuals, strict=True):
            if actual > 0:
                error = abs(pred - actual) / actual * 100
                errors.append(error)

        if not errors:
            return {"detected": False, "checked": True}

        mean_error_pct = np.mean(errors)

        # Drift severity levels
        drift_detected = mean_error_pct > 20  # >20% error indicates possible drift
        severe_drift = mean_error_pct > 50  # >50% error is severe

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
            "detected": drift_detected,
            "error_pct": mean_error_pct,
            "severity": (
                "severe"
                if severe_drift
                else ("moderate" if drift_detected else "normal")
            ),
            "checked": True,
        }

    def should_trigger_retraining(self, drift_severity: str, error_pct: float) -> dict:
        """
        FIX (PR#16): Determine if retraining should be triggered based on drift severity.

        Returns: dict with 'trigger', 'reason', 'suggested_action'
        """
        current_time = time.time()

        # Track severe drift duration
        if not hasattr(self, "_severe_drift_start_time"):
            self._severe_drift_start_time = None
            self._retraining_triggered = False

        if drift_severity == "severe" and error_pct > 50:
            if self._severe_drift_start_time is None:
                self._severe_drift_start_time = current_time
                logger.info("Severe drift detected, starting 1-hour retraining timer")

            # Check if severe drift has persisted for >1 hour
            drift_duration = current_time - self._severe_drift_start_time
            if drift_duration > 3600 and not self._retraining_triggered:  # 1 hour
                self._retraining_triggered = True
                return {
                    "trigger": True,
                    "reason": f"Severe drift ({error_pct:.1f}%) persisted for {drift_duration / 60:.0f} minutes",
                    "suggested_action": "trigger_retraining_job",
                    "drift_duration_minutes": drift_duration / 60,
                }
        else:
            # Reset timer if drift clears
            if self._severe_drift_start_time is not None:
                logger.info(
                    f"Drift cleared after {(current_time - self._severe_drift_start_time) / 60:.0f} minutes"
                )
            self._severe_drift_start_time = None

        return {
            "trigger": False,
            "reason": "No sustained severe drift detected",
            "suggested_action": "continue_monitoring",
        }

    def reset_retraining_flag(self):
        """Call after retraining job completes successfully."""
        self._retraining_triggered = False
        self._severe_drift_start_time = None
        logger.info("Retraining flag reset - monitoring resumed")


def _load_ai_edge_litert_interpreter():
    """Load the LiteRT interpreter from the most common import locations."""
    try:
        from ai_edge_litert.interpreter import Interpreter

        return Interpreter
    except Exception:
        # Fallback: try direct import
        from ai_edge_litert import Interpreter

        return Interpreter
