# operator/predictor.py — TFLite inference wrapper
"""Wraps the LSTM model for online prediction with rolling history."""

import logging
import numpy as np
import joblib

from collections import deque
from config import LOOKBACK_STEPS

logger = logging.getLogger("ppa.predictor")

NUM_FEATURES = 14


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
        except Exception as e:
            logger.error(f"Failed to load model/scaler: {e}")
            self.interpreter = None
            self.scaler = None

    def paths_match(self, model_path: str, scaler_path: str) -> bool:
        """Check if this predictor was loaded from the given paths."""
        return self.model_path == model_path and self.scaler_path == scaler_path

    def update(self, features: dict):
        """Append a feature vector to the rolling window."""
        row = np.array([
            features["requests_per_second"],
            features["cpu_usage_percent"],
            features["memory_usage_bytes"],
            features["latency_p95_ms"],
            features["active_connections"],
            features["error_rate"],
            features["cpu_acceleration"],
            features["rps_acceleration"],
            features["current_replicas"],
            features["hour_sin"],
            features["hour_cos"],
            features["dow_sin"],
            features["dow_cos"],
            features["is_weekend"],
        ], dtype=np.float32)
        self.history.append(row)

    def ready(self) -> bool:
        return (
            len(self.history) >= LOOKBACK_STEPS
            and self.interpreter is not None
            and self.scaler is not None
        )

    def predict(self) -> float:
        """Run TFLite inference on the rolling window, return predicted RPS."""
        if not self.ready():
            return 0.0

        window = np.array(self.history, dtype=np.float32)[-LOOKBACK_STEPS:]
        scaled = self.scaler.transform(window)
        input_data = scaled.reshape(1, LOOKBACK_STEPS, NUM_FEATURES).astype(np.float32)

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()
        output = self.interpreter.get_tensor(self.output_details[0]["index"])

        # Inverse-transform the prediction (only the RPS column)
        predicted_scaled = output[0][0]
        dummy = np.zeros((1, NUM_FEATURES), dtype=np.float32)
        dummy[0, 0] = predicted_scaled
        predicted_rps = self.scaler.inverse_transform(dummy)[0, 0]

        return max(0.0, float(predicted_rps))
