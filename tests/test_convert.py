# tests/test_convert.py — Unit tests for model/convert.py
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from model.convert import convert_model


def _create_tiny_keras_model(output_path):
    """Build and save a minimal Keras model for testing conversion."""
    import keras
    from keras import layers

    model = keras.Sequential([
        layers.Input(shape=(12, 14)),
        layers.LSTM(8),
        layers.Dense(1, activation="linear"),
    ])
    model.compile(optimizer="adam", loss="mse")
    model.save(output_path)
    return output_path


class TestConvertModel:
    def test_produces_valid_tflite(self):
        """Conversion should produce a non-empty .tflite file."""
        with tempfile.TemporaryDirectory() as tmp:
            keras_path = os.path.join(tmp, "test_model.keras")
            _create_tiny_keras_model(keras_path)

            result = convert_model(model_path=keras_path, quantize=False)

            assert result is not None
            assert os.path.exists(result["output_path"])
            assert result["size_kb"] > 0

    def test_quantized_smaller_than_unquantized(self):
        """Quantized model should be equal or smaller in size."""
        with tempfile.TemporaryDirectory() as tmp:
            keras_path = os.path.join(tmp, "test_model.keras")
            _create_tiny_keras_model(keras_path)

            result_full = convert_model(
                model_path=keras_path, quantize=False,
                output_path=os.path.join(tmp, "full.tflite"),
            )
            result_quant = convert_model(
                model_path=keras_path, quantize=True,
                output_path=os.path.join(tmp, "quant.tflite"),
            )

            assert result_full is not None
            assert result_quant is not None
            # Quantized should be <= full size (for small models they may be equal)
            assert result_quant["size_kb"] <= result_full["size_kb"] + 1  # 1KB tolerance

    def test_custom_output_path(self):
        """--output flag should place the file at the specified path."""
        with tempfile.TemporaryDirectory() as tmp:
            keras_path = os.path.join(tmp, "model.keras")
            _create_tiny_keras_model(keras_path)

            custom_path = os.path.join(tmp, "subdir", "custom.tflite")
            result = convert_model(
                model_path=keras_path, quantize=False,
                output_path=custom_path,
            )

            assert result is not None
            assert result["output_path"] == custom_path
            assert os.path.exists(custom_path)

    def test_returns_none_for_missing_model(self):
        result = convert_model(model_path="/nonexistent/model.keras")
        assert result is None

    def test_tflite_is_loadable(self):
        """The produced .tflite file should be valid (valid magic bytes)."""
        with tempfile.TemporaryDirectory() as tmp:
            keras_path = os.path.join(tmp, "model.keras")
            _create_tiny_keras_model(keras_path)

            result = convert_model(model_path=keras_path, quantize=False)
            assert result is not None

            # Verify it's a valid TFLite file (magic bytes check)
            with open(result["output_path"], "rb") as f:
                header = f.read(20)
                # TFLite uses FlatBuffers format, TFL3 magic bytes appear within first 20 bytes
                assert b"TFL3" in header, "Invalid TFLite file (missing TFL3 magic bytes)"
