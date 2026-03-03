# model/convert.py — Keras → TFLite + quantize
"""Convert trained .keras model to .tflite with optional int8 quantization.

TODO: Implement after train.py produces a model.

Pipeline:
    1. Load .keras model from artifacts/
    2. Convert to TFLite format
    3. Apply float16 or int8 quantization
    4. Save .tflite to artifacts/
"""
