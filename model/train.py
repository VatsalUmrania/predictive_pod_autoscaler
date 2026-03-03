# model/train.py — LSTM training pipeline
"""CSV → trained Keras LSTM model.

TODO: Implement after collecting 3-7 days of training data.

Pipeline:
    1. Load CSV from data-collection/training-data/
    2. Normalize with MinMaxScaler
    3. Create sliding windows (LOOKBACK_STEPS × 9 features)
    4. Train LSTM (2 layers, 64 units each)
    5. Save .keras model + scaler.pkl to artifacts/
"""
