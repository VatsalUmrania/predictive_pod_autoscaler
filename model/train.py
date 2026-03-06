# model/train.py — LSTM training pipeline
import argparse
import os
import sys
from pathlib import Path

import joblib
import keras
import numpy as np
import pandas as pd
from keras import layers
from sklearn.preprocessing import MinMaxScaler

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS

LOOKBACK_STEPS = 12
TARGET_COL = TARGET_COLUMNS[0]


def create_dataset_from_segments(df, feature_cols, target_col, scaler, lookback):
    """Build sliding windows respecting segment_id boundaries."""
    X_all, y_all = [], []

    if "segment_id" in df.columns:
        for _, seg in df.groupby("segment_id"):
            seg_scaled = scaler.transform(seg[feature_cols])
            seg_targets = seg[target_col].values
            for i in range(len(seg_scaled) - lookback):
                X_all.append(seg_scaled[i:(i + lookback)])
                y_all.append(seg_targets[i + lookback - 1])
    else:
        scaled_features = scaler.transform(df[feature_cols])
        target_vals = df[target_col].values
        for i in range(len(scaled_features) - lookback):
            X_all.append(scaled_features[i:(i + lookback)])
            y_all.append(target_vals[i + lookback - 1])

    return np.array(X_all), np.array(y_all)


def train_model(csv_path="data-collection/training-data/training_data.csv", lookback=LOOKBACK_STEPS, epochs=50):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
    df = df.dropna(subset=FEATURE_COLUMNS + [TARGET_COL])

    if len(df) < lookback + 10:
        print("Not enough data to train. Need at least", lookback + 10, "rows.")
        return

    print(f"Training on {len(df)} rows.")
    if "segment_id" in df.columns:
        print(f"Found {df['segment_id'].nunique()} continuous segment(s).")

    scaler = MinMaxScaler()
    scaler.fit(df[FEATURE_COLUMNS])
    X, y = create_dataset_from_segments(df, FEATURE_COLUMNS, TARGET_COL, scaler, lookback)

    split_idx = int(len(X) * 0.8)
    X_train, y_train = X[:split_idx], y[:split_idx]
    X_val, y_val = X[split_idx:], y[split_idx:]

    print(f"X_train shape: {X_train.shape}, y_train shape: {y_train.shape}")

    model = keras.Sequential([
        layers.Input(shape=(lookback, len(FEATURE_COLUMNS))),
        layers.LSTM(64, return_sequences=True),
        layers.LSTM(32),
        layers.Dense(16, activation="relu"),
        layers.Dense(1, activation="linear"),
    ])

    model.compile(optimizer="adam", loss="mse", metrics=["mae"])
    model.summary()

    print("Training model...")
    model.fit(
        X_train,
        y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=32,
        callbacks=[keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True)],
    )

    os.makedirs("model/artifacts", exist_ok=True)
    model.save("model/artifacts/ppa_model.keras")
    joblib.dump(scaler, "model/artifacts/scaler.pkl")
    print("Saved model to model/artifacts/ppa_model.keras")
    print("Saved scaler to model/artifacts/scaler.pkl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default="data-collection/training-data/training_data.csv")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument("--epochs", type=int, default=50)
    args = parser.parse_args()

    train_model(args.csv, args.lookback, args.epochs)
