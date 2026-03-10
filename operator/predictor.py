# operator/predictor.py — TFLite inference wrapper
"""Wrap the LSTM model for online prediction with rolling history."""

import logging
import sys
from collections import deque
from pathlib import Path

import joblib
import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS, NUM_FEATURES
from config import LOOKBACK_STEPS

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

        self._try_load()

    def _try_load(self):
        """Attempt to load model, scaler, and target scaler. Idempotent."""
        if self.interpreter is not None and self.scaler is not None:
            return  # already loaded
        try:
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
            self.scaler = joblib.load(self.scaler_path)

            # Target scaler: inverse-transforms model output [0,1] → raw RPS
            self.target_scaler = None
            if self.target_scaler_path:
                self.target_scaler = joblib.load(self.target_scaler_path)
                logger.info(f"Loaded target scaler from {self.target_scaler_path}")

            logger.info(f"Loaded model from {self.model_path}, scaler from {self.scaler_path}")
            self._load_failed = False
        except Exception as exc:
            logger.error(f"Failed to load model/scaler: {exc}")
            self.interpreter = None
            self.scaler = None
            self.target_scaler = None
            self._load_failed = True

    def paths_match(self, model_path: str, scaler_path: str, target_scaler_path: str | None = None) -> bool:
        return (
            self.model_path == model_path
            and self.scaler_path == scaler_path
            and self.target_scaler_path == target_scaler_path
        )

    def update(self, features: dict):
        row = np.array([features[name] for name in FEATURE_COLUMNS], dtype=np.float32)
        self.history.append(row)

    def ready(self) -> bool:
        # Retry loading if previous attempt failed (e.g. missing dependency installed later)
        if self._load_failed:
            self._try_load()
        return (
            len(self.history) >= LOOKBACK_STEPS
            and self.interpreter is not None
            and self.scaler is not None
        )

    def predict(self) -> float:
        if not self.ready():
            return 0.0

        window = np.array(self.history, dtype=np.float32)[-LOOKBACK_STEPS:]
        scaled = self.scaler.transform(window)
        input_data = scaled.reshape(1, LOOKBACK_STEPS, NUM_FEATURES).astype(np.float32)

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()
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
