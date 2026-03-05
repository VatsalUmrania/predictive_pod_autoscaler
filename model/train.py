# model/train.py — LSTM training pipeline
import os
import argparse
import pandas as pd
import numpy as np
import joblib
import keras
from sklearn.preprocessing import MinMaxScaler
from keras import layers

LOOKBACK_STEPS = 12  # 12 steps x 15s = 3 minutes of history? Wait, in config.py lookback is 60m right now (12 * 5m), but for 15s data 12 steps is 3m. Let's make it parameterizable but default to 12.
# Let's read it from the dataframe columns.
FEATURE_COLS = [
    "requests_per_second",
    "cpu_usage_percent",
    "memory_usage_bytes",
    "latency_p95_ms",
    "active_connections",
    "error_rate",
    "cpu_acceleration",
    "rps_acceleration",
    "current_replicas",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend"
]
TARGET_COL = "rps_t5"  # Use 5m future target for now

def create_dataset_from_segments(df, feature_cols, target_col, scaler, lookback):
    """Build sliding windows respecting segment_id boundaries.
    
    Windows never cross gaps between sessions, preventing the LSTM
    from learning on data that jumps overnight.
    """
    scaled_features = scaler.transform(df[feature_cols])
    target_vals = df[target_col].values
    
    X_all, y_all = [], []
    
    # If segment_id exists, build windows per-segment
    if "segment_id" in df.columns:
        for _, seg in df.groupby("segment_id"):
            seg_scaled = scaler.transform(seg[feature_cols])
            seg_targets = seg[target_col].values
            for i in range(len(seg_scaled) - lookback):
                X_all.append(seg_scaled[i:(i + lookback)])
                y_all.append(seg_targets[i + lookback - 1])
    else:
        # Fallback: treat entire dataset as one segment
        for i in range(len(scaled_features) - lookback):
            X_all.append(scaled_features[i:(i + lookback)])
            y_all.append(target_vals[i + lookback - 1])
    
    return np.array(X_all), np.array(y_all)

def train_model(csv_path="data-collection/training-data/training_data.csv", lookback=12, epochs=50):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return
        
    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
    df = df.dropna(subset=FEATURE_COLS + [TARGET_COL])
    
    if len(df) < lookback + 10:
        print("Not enough data to train. Need at least", lookback + 10, "rows.")
        return
        
    print(f"Training on {len(df)} rows.")
    if "segment_id" in df.columns:
        n_segments = df["segment_id"].nunique()
        print(f"Found {n_segments} continuous segment(s).")
    
    # Scale features
    scaler = MinMaxScaler()
    scaler.fit(df[FEATURE_COLS])
    
    X, y = create_dataset_from_segments(df, FEATURE_COLS, TARGET_COL, scaler, lookback)
    
    # Split train/val
    split_idx = int(len(X) * 0.8)
    X_train, y_train = X[:split_idx], y[:split_idx]
    X_val, y_val = X[split_idx:], y[split_idx:]
    
    print(f"X_train shape: {X_train.shape}, y_train shape: {y_train.shape}")
    
    # Build LSTM
    model = keras.Sequential([
        layers.Input(shape=(lookback, len(FEATURE_COLS))),
        layers.LSTM(64, return_sequences=True),
        layers.LSTM(32),
        layers.Dense(16, activation="relu"),
        layers.Dense(1, activation="linear")
    ])
    
    model.compile(optimizer="adam", loss="mse", metrics=["mae"])
    model.summary()
    
    # Train
    print("Training model...")
    history = model.fit(
        X_train, y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=32,
        callbacks=[keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)]
    )
    
    # Save artifacts
    os.makedirs("model/artifacts", exist_ok=True)
    model.save("model/artifacts/ppa_model.keras")
    joblib.dump(scaler, "model/artifacts/scaler.pkl")
    print("Saved model to model/artifacts/ppa_model.keras")
    print("Saved scaler to model/artifacts/scaler.pkl")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default="data-collection/training-data/training_data.csv")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--epochs", type=int, default=50)
    args = parser.parse_args()
    
    train_model(args.csv, args.lookback, args.epochs)
