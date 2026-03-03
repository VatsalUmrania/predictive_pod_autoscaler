# operator/predictor.py — TFLite inference wrapper
"""Load a TFLite model, preprocess input, and return predicted load."""

import numpy as np
import joblib

try:
    import tflite_runtime.interpreter as tflite
except ImportError:
    import tensorflow.lite as tflite

from config import MODEL_PATH, SCALER_PATH, LOOKBACK_STEPS


class Predictor:
    """Wraps a TFLite LSTM model for single-step inference."""

    def __init__(self):
        self.interpreter = tflite.Interpreter(model_path=MODEL_PATH)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()
        self.scaler = joblib.load(SCALER_PATH)
        self.history = []  # rolling window of feature vectors

    def update(self, features: dict):
        """Append a feature vector to the rolling window."""
        row = np.array([
            features["requests_per_second"],
            features["latency_p95_ms"],
            features["cpu_usage_percent"],
            features["memory_usage_bytes"],
            features["hour_sin"],
            features["hour_cos"],
            features["dow_sin"],
            features["dow_cos"],
            features["current_replicas"],
        ], dtype=np.float32)
        self.history.append(row)

        # Keep only LOOKBACK_STEPS
        if len(self.history) > LOOKBACK_STEPS:
            self.history = self.history[-LOOKBACK_STEPS:]

    def ready(self) -> bool:
        """True when enough history has accumulated for inference."""
        return len(self.history) >= LOOKBACK_STEPS

    def predict(self) -> float:
        """Run inference and return predicted load (requests_per_second)."""
        if not self.ready():
            return 0.0

        window = np.array(self.history[-LOOKBACK_STEPS:], dtype=np.float32)
        scaled = self.scaler.transform(window)  # (LOOKBACK_STEPS, 9)
        input_data = scaled.reshape(1, LOOKBACK_STEPS, 9).astype(np.float32)

        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()
        output = self.interpreter.get_tensor(self.output_details[0]["index"])

        # Inverse-transform the prediction (only the RPS column)
        predicted_scaled = output[0][0]
        # Create a dummy row to inverse-transform just the first column
        dummy = np.zeros((1, 9), dtype=np.float32)
        dummy[0, 0] = predicted_scaled
        predicted_rps = self.scaler.inverse_transform(dummy)[0, 0]

        return max(0.0, float(predicted_rps))
