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

    def __init__(self, model_path: str, scaler_path: str):
        self.model_path = model_path
        self.scaler_path = scaler_path
        self.history: deque = deque(maxlen=LOOKBACK_STEPS)

        try:
            import tflite_runtime.interpreter as tflite
            self.interpreter = tflite.Interpreter(model_path=model_path)
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()
            self.scaler = joblib.load(scaler_path)
            logger.info(f"Loaded model from {model_path}, scaler from {scaler_path}")
        except Exception as exc:
            logger.error(f"Failed to load model/scaler: {exc}")
            self.interpreter = None
            self.scaler = None

    def paths_match(self, model_path: str, scaler_path: str) -> bool:
        return self.model_path == model_path and self.scaler_path == scaler_path

    def update(self, features: dict):
        row = np.array([features[name] for name in FEATURE_COLUMNS], dtype=np.float32)
        self.history.append(row)

    def ready(self) -> bool:
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

        predicted_scaled = output[0][0]
        dummy = np.zeros((1, NUM_FEATURES), dtype=np.float32)
        dummy[0, 0] = predicted_scaled
        predicted_rps = self.scaler.inverse_transform(dummy)[0, 0]
        return max(0.0, float(predicted_rps))
