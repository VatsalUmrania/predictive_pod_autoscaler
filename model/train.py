# model/train.py — LSTM training pipeline (multi-horizon)
import argparse
import json
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
DEFAULT_TARGET = TARGET_COLUMNS[0]  # rps_t3m


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


def build_model(lookback, num_features):
    """Build the LSTM architecture."""
    model = keras.Sequential([
        layers.Input(shape=(lookback, num_features)),
        layers.LSTM(64, return_sequences=True, unroll=True),  # unroll=True: eliminates FlexTensorList* ops in TFLite
        layers.LSTM(32, unroll=True),
        layers.Dense(16, activation="relu"),
        layers.Dense(1, activation="linear"),
    ])
    model.compile(optimizer="adam", loss="mse", metrics=["mae"])
    return model


def train_model(
    csv_path="data-collection/training-data/training_data_v2.csv",
    lookback=LOOKBACK_STEPS,
    epochs=50,
    target_col=DEFAULT_TARGET,
    test_split=0.1,
    output_dir="model/artifacts",
):
    """Train an LSTM model for a single target horizon.

    Returns:
        dict with keys: model, scaler, history, metrics, artifact_paths
        or None on failure.
    """
    if target_col not in TARGET_COLUMNS:
        print(f"Error: target '{target_col}' not in {TARGET_COLUMNS}")
        return None

    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return None

    print(f"\n{'='*60}")
    print(f"Training target: {target_col}")
    print(f"{'='*60}")

    print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
    df = df.dropna(subset=FEATURE_COLUMNS + [target_col])

    if len(df) < lookback + 10:
        print("Not enough data to train. Need at least", lookback + 10, "rows.")
        return None

    print(f"Total rows after cleaning: {len(df)}")
    if "segment_id" in df.columns:
        print(f"Found {df['segment_id'].nunique()} continuous segment(s).")

    scaler = MinMaxScaler()
    scaler.fit(df[FEATURE_COLUMNS])
    X, y = create_dataset_from_segments(df, FEATURE_COLUMNS, target_col, scaler, lookback)

    # 3-way chronological split: train / val / test
    n = len(X)
    test_start = int(n * (1 - test_split))
    val_start = int(test_start * 0.8)  # 80% of non-test data for training

    X_train, y_train = X[:val_start], y[:val_start]
    X_val, y_val = X[val_start:test_start], y[val_start:test_start]
    X_test, y_test = X[test_start:], y[test_start:]

    print(f"Split sizes — train: {len(X_train)}, val: {len(X_val)}, test: {len(X_test)}")

    model = build_model(lookback, len(FEATURE_COLUMNS))
    model.summary()

    print("Training model...")
    history = model.fit(
        X_train,
        y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=32,
        callbacks=[
            keras.callbacks.EarlyStopping(patience=5, restore_best_weights=True, verbose=1),
            keras.callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=3, min_lr=1e-6, verbose=1),
        ],
    )

    # Save artifacts with horizon-specific names
    os.makedirs(output_dir, exist_ok=True)
    model_path = os.path.join(output_dir, f"ppa_model_{target_col}.keras")
    scaler_path = os.path.join(output_dir, f"scaler_{target_col}.pkl")
    meta_path = os.path.join(output_dir, f"split_meta_{target_col}.json")

    model.save(model_path)
    joblib.dump(scaler, scaler_path)

    # Save split metadata so evaluate.py can reproduce the exact test set
    split_meta = {
        "target_col": target_col,
        "lookback": lookback,
        "total_windows": n,
        "val_start_idx": val_start,
        "test_start_idx": test_start,
        "train_size": len(X_train),
        "val_size": len(X_val),
        "test_size": len(X_test),
        "csv_path": csv_path,
    }
    with open(meta_path, "w") as f:
        json.dump(split_meta, f, indent=2)

    print(f"Saved model  → {model_path}")
    print(f"Saved scaler → {scaler_path}")
    print(f"Saved meta   → {meta_path}")

    # Compute final metrics on validation set
    val_loss, val_mae = model.evaluate(X_val, y_val, verbose=0)
    metrics = {
        "val_loss": float(val_loss),
        "val_mae": float(val_mae),
        "epochs_run": len(history.history["loss"]),
    }

    return {
        "model": model,
        "scaler": scaler,
        "history": history,
        "metrics": metrics,
        "artifact_paths": {
            "model": model_path,
            "scaler": scaler_path,
            "meta": meta_path,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train LSTM model for a target horizon")
    parser.add_argument("--csv", type=str, default="data-collection/training-data/training_data_v2.csv")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--target", type=str, default=DEFAULT_TARGET,
                        choices=TARGET_COLUMNS, help="Target column to predict")
    parser.add_argument("--test-split", type=float, default=0.1,
                        help="Fraction of data to hold out for testing (default: 0.1)")
    parser.add_argument("--output-dir", type=str, default="model/artifacts")
    args = parser.parse_args()

    result = train_model(
        csv_path=args.csv,
        lookback=args.lookback,
        epochs=args.epochs,
        target_col=args.target,
        test_split=args.test_split,
        output_dir=args.output_dir,
    )
    if result:
        print(f"\nTraining complete. Val MAE: {result['metrics']['val_mae']:.4f}")
    else:
        sys.exit(1)
