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

from ppa.common.constants import CAPACITY_PER_POD
from ppa.common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
from ppa.model.artifacts import artifact_dir, keras_model_path, scaler_path, target_scaler_path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LOOKBACK_STEPS = 60  # 60 minutes of historical data to predict the next time step


# Asymmetric loss removed: the model now trains with symmetric MSE.
# Safety margin is applied explicitly in scaler.py via the `safetyBuffer`
# CRD parameter, which keeps the model unbiased and the safety headroom
# configurable per deployment without retraining.


DEFAULT_TARGET = TARGET_COLUMNS[0]  # rps_t3m


def create_dataset_from_segments(df, feature_cols, target_col, scaler, lookback):
    """Build sliding windows respecting segment_id boundaries."""
    x_all, y_all = [], []

    if "segment_id" in df.columns:
        for _, seg in df.groupby("segment_id"):
            seg_scaled = scaler.transform(seg[feature_cols])
            seg_targets = seg[target_col].values
            for i in range(len(seg_scaled) - lookback):
                x_all.append(seg_scaled[i : (i + lookback)])
                y_all.append(seg_targets[i + lookback - 1])
    else:
        scaled_features = scaler.transform(df[feature_cols])
        target_vals = df[target_col].values
        for i in range(len(scaled_features) - lookback):
            x_all.append(scaled_features[i : (i + lookback)])
            y_all.append(target_vals[i + lookback - 1])

    return np.array(x_all), np.array(y_all)


def build_model(lookback, num_features):
    """Build the LSTM architecture with regularisation."""
    model = keras.Sequential(
        [
            layers.Input(shape=(lookback, num_features)),
            layers.LSTM(
                128, return_sequences=True, unroll=True
            ),  # unroll=True: eliminates FlexTensorList* ops in TFLite
            layers.Dropout(0.2),
            layers.LSTM(64, unroll=True),
            layers.Dropout(0.2),
            layers.Dense(32, activation="relu"),
            layers.Dense(1, activation="linear"),
        ]
    )
    optimizer = keras.optimizers.Adam(learning_rate=1e-3, clipnorm=1.0)
    model.compile(optimizer=optimizer, loss="mse", metrics=["mae"])
    return model


def train_model(
    csv_path="data/training-data/training_data_v2.csv",
    lookback=LOOKBACK_STEPS,
    epochs=50,
    target_col=DEFAULT_TARGET,
    app_name="test-app",
    namespace="default",
    test_split=0.1,
    output_dir="data/artifacts",
    target_floor=5.0,
    early_stopping_patience=15,
    early_stopping_min_delta=1e-4,
    on_data_loaded=None,
    on_model_created=None,
    on_epoch_complete=None,
    on_batch_complete=None,
    on_artifacts_saved=None,
    verbose=None,
    suppress_info=False,
    min_replicas=2,
    max_replicas=20,
    capacity=CAPACITY_PER_POD,
):
    """Train an LSTM model for a single target horizon.

    Args:
        csv_path: Path to training CSV
        lookback: Lookback window steps
        epochs: Number of training epochs
        target_col: Target column to predict
        app_name: Application name
        namespace: Kubernetes namespace
        test_split: Fraction of data for testing
        output_dir: Directory to save artifacts
        target_floor: Minimum value for rps_* targets
        early_stopping_patience: Early stopping patience
        early_stopping_min_delta: Early stopping minimum delta
        on_data_loaded: Optional callback(rows, segments, train_size, val_size, test_size) fired after data load
        on_model_created: Optional callback(params, trainable_params) fired after model build
        on_epoch_complete: Optional callback(epoch, loss, val_loss, mae) fired after each epoch
        on_batch_complete: Optional callback(batch, logs) fired after each batch
        on_artifacts_saved: Optional callback(paths_dict) fired after artifact save
        verbose: Keras verbosity (0=silent, 1=progress, 2=one line per epoch). If None, auto-set to 0 when callbacks provided.
        suppress_info: Suppress informational print statements (set automatically to True when callbacks provided)

    Returns:
        dict with keys: model, scaler, history, metrics, artifact_paths
        or None on failure.
    """
    # Auto-detect verbose mode: silent if callbacks provided
    if verbose is None:
        verbose = 0 if (on_epoch_complete or on_model_created) else 1

    # Auto-suppress info when callbacks are provided
    if suppress_info is False and (on_epoch_complete or on_model_created):
        suppress_info = True

    if target_col not in TARGET_COLUMNS:
        print(f"Error: target '{target_col}' not in {TARGET_COLUMNS}")
        return None

    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return None

    if not suppress_info:
        print(f"\n{'=' * 60}")
        print(f"Training target: {target_col}")
        print(f"{'=' * 60}")

    if not suppress_info:
        print(f"Loading data from {csv_path}...")
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
    df = df.dropna(subset=FEATURE_COLUMNS + [target_col])

    if target_col.startswith("rps_") and target_floor is not None:
        df[target_col] = df[target_col].clip(lower=float(target_floor))

    if len(df) < lookback + 10:
        print("Not enough data to train. Need at least", lookback + 10, "rows.")
        return None

    if not suppress_info:
        print(f"Total rows after cleaning: {len(df)}")
    num_segments = df["segment_id"].nunique() if "segment_id" in df.columns else 1
    if not suppress_info and "segment_id" in df.columns:
        print(f"Found {num_segments} continuous segment(s).")

    scaler = MinMaxScaler()
    scaler.fit(df[FEATURE_COLUMNS])
    x, y = create_dataset_from_segments(df, FEATURE_COLUMNS, target_col, scaler, lookback)

    # Shuffle windows before splitting so val/test see patterns from all
    # segments/time-periods.  Each window is self-contained (lookback steps)
    # so shuffling does NOT leak future→past; it just ensures the val set
    # is representative of the full traffic distribution.
    rng = np.random.RandomState(42)
    shuffle_idx = rng.permutation(len(x))
    x, y = x[shuffle_idx], y[shuffle_idx]

    # 3-way split: train / val / test
    n = len(x)
    test_start = int(n * (1 - test_split))
    val_start = int(test_start * 0.8)  # 80% of non-test data for training

    x_train, y_train_raw = x[:val_start], y[:val_start]
    x_val, y_val_raw = x[val_start:test_start], y[val_start:test_start]
    x_test, _ = x[test_start:], y[test_start:]

    # Scale targets to [0,1] using train split only (avoids leakage).
    target_scaler = MinMaxScaler()
    y_train = target_scaler.fit_transform(y_train_raw.reshape(-1, 1)).flatten()
    y_val = target_scaler.transform(y_val_raw.reshape(-1, 1)).flatten()

    if not suppress_info:
        print(f"Split sizes — train: {len(x_train)}, val: {len(x_val)}, test: {len(x_test)}")

    # Fire on_data_loaded callback
    if on_data_loaded:
        on_data_loaded(
            rows=len(df),
            segments=num_segments,
            train_size=len(x_train),
            val_size=len(x_val),
            test_size=len(x_test),
        )

    model = build_model(lookback, len(FEATURE_COLUMNS))
    if verbose:
        model.summary()

    # Fire on_model_created callback
    if on_model_created:
        total_params = model.count_params()
        trainable_params = sum(
            np.prod(w.shape) for w in model.trainable_weights
        )
        on_model_created(total_params=total_params, trainable_params=trainable_params)

    if not suppress_info:
        print("Training model...")

    # Custom callback for epoch completion
    class EpochCallbackBridge(keras.callbacks.Callback):
        def on_epoch_end(self, epoch, logs=None):
            if on_epoch_complete and logs:
                on_epoch_complete(
                    epoch=epoch + 1,
                    loss=float(logs.get("loss", 0)),
                    val_loss=float(logs.get("val_loss", 0)),
                    mae=float(logs.get("mae", 0)),
                )

        def on_batch_end(self, batch, logs=None):
            if on_batch_complete and logs:
                on_batch_complete(batch=batch + 1, logs=logs)

    callbacks_list = [
        keras.callbacks.EarlyStopping(
            patience=early_stopping_patience,
            min_delta=early_stopping_min_delta,
            restore_best_weights=True,
            verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss",
            factor=0.5,
            patience=10,
            min_lr=1e-5,
            min_delta=0.0005,
            verbose=1,
        ),
    ]
    if on_epoch_complete or on_batch_complete:
        callbacks_list.append(EpochCallbackBridge())

    history = model.fit(
        x_train,
        y_train,
        validation_data=(x_val, y_val),
        epochs=epochs,
        batch_size=32,
        callbacks=callbacks_list,
        verbose=verbose,
    )

    # Save artifacts with structured app/namespace/target names
    out_dir = artifact_dir(app_name, namespace, target_col, Path(output_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = keras_model_path(app_name, namespace, target_col, Path(output_dir))
    scaler_file = scaler_path(app_name, namespace, target_col, Path(output_dir))
    target_scaler_file = target_scaler_path(app_name, namespace, target_col, Path(output_dir))
    meta_path = out_dir / f"split_meta_{target_col}.json"

    model.save(model_path)
    joblib.dump(scaler, scaler_file)
    joblib.dump(target_scaler, target_scaler_file)

    # Save split metadata so evaluate.py can reproduce the exact test set
    split_meta = {
        "target_col": target_col,
        "lookback": lookback,
        "total_windows": n,
        "val_start_idx": val_start,
        "test_start_idx": test_start,
        "train_size": len(x_train),
        "val_size": len(x_val),
        "test_size": len(x_test),
        "csv_path": csv_path,
    }
    with open(meta_path, "w") as f:
        json.dump(split_meta, f, indent=2)

    if not suppress_info:
        print(f"Saved model          → {model_path}")
        print(f"Saved feature scaler → {scaler_file}")
        print(f"Saved target scaler  → {target_scaler_file}")
        print(f"Saved meta           → {meta_path}")

    # Fire on_artifacts_saved callback
    if on_artifacts_saved:
        on_artifacts_saved(
            paths={
                "model": str(model_path),
                "scaler": str(scaler_file),
                "target_scaler": str(target_scaler_file),
                "meta": str(meta_path),
            }
        )

    # Compute final metrics on validation set
    val_loss, val_mae_keras = model.evaluate(x_val, y_val, verbose=0)

    # Get validation predictions to compute accuracy metrics
    y_val_pred = model.predict(x_val, verbose=0)
    y_val_pred = y_val_pred.flatten()

    # Inverse transform predictions and actuals to original scale
    y_val_actual = target_scaler.inverse_transform(y_val.reshape(-1, 1)).flatten()
    y_val_pred_scaled = target_scaler.inverse_transform(y_val_pred.reshape(-1, 1)).flatten()

# Import metric functions here to avoid circular import
    from ppa.model.evaluate import (
        compute_mae,
        compute_mape,
        compute_rmse,
        compute_scaling_stats,
        compute_smape,
        rps_to_replicas,
    )

    # Compute accuracy metrics
    mae = compute_mae(y_val_actual, y_val_pred_scaled)
    mape = compute_mape(y_val_actual, y_val_pred_scaled)
    smape = compute_smape(y_val_actual, y_val_pred_scaled)
    rmse = compute_rmse(y_val_actual, y_val_pred_scaled)

    # PPA vs HPA comparison on validation set
    ppa_replicas = rps_to_replicas(y_val_pred_scaled, capacity, min_replicas, max_replicas)
    hpa_replicas = rps_to_replicas(y_val_actual, capacity, min_replicas, max_replicas)
    ppa_stats = compute_scaling_stats(y_val_actual, ppa_replicas, capacity, "ppa")
    hpa_stats = compute_scaling_stats(y_val_actual, hpa_replicas, capacity, "hpa")

    replica_savings = (
        1 - ppa_stats["ppa_avg_replicas"] / max(hpa_stats["hpa_avg_replicas"], 1e-6)
    ) * 100

    metrics = {
        "val_loss": float(val_loss),
        "val_mae": float(val_mae_keras),
        "val": {
            "mae": round(mae, 4),
            "mape": round(mape, 4),
            "smape": round(smape, 4),
            "rmse": round(rmse, 4),
        },
        "data": {
            "train_size": len(x_train),
            "val_size": len(x_val),
            "test_size": len(x_test),
        },
        "epochs_run": len(history.history["loss"]),
    }

    # Save training metrics summary with accuracy metrics and HPA comparison
    metrics_summary = {
        "target": target_col,
        "mae": round(mae, 4),
        "mape": round(mape, 4),
        "smape": round(smape, 4),
        "rmse": round(rmse, 4),
        "epochs_run": len(history.history["loss"]),
        "train_size": len(x_train),
        "val_size": len(x_val),
        "test_size": len(x_test),
        "lookback": lookback,
        **ppa_stats,
        **hpa_stats,
        "replica_savings_pct": round(replica_savings, 2),
    }
    train_summary_path = out_dir / f"train_summary_{target_col}.json"
    with open(train_summary_path, "w") as f:
        json.dump(metrics_summary, f, indent=2)

    if not suppress_info:
        print(f"Saved training summary → {train_summary_path}")
        # Print HPA vs PPA comparison table
        print(f"\n  {'Metric':<30} {'PPA':>10} {'HPA':>10}")
        print(f"  {'─' * 52}")
        print(f"  {'Avg Replicas':<30} {ppa_stats['ppa_avg_replicas']:>10.2f} {hpa_stats['hpa_avg_replicas']:>10.2f}")
        print(f"  {'Over-provisioning %':<30} {ppa_stats['ppa_over_prov_pct']:>10.1f} {hpa_stats['hpa_over_prov_pct']:>10.1f}")
        print(f"  {'Under-provisioning %':<30} {ppa_stats['ppa_under_prov_pct']:>10.1f} {hpa_stats['hpa_under_prov_pct']:>10.1f}")
        print(f"  {'Wasted Capacity (avg RPS)':<30} {ppa_stats['ppa_wasted_capacity_avg']:>10.1f} {hpa_stats['hpa_wasted_capacity_avg']:>10.1f}")
        print(f"  {'Replica Savings':<30} {replica_savings:>10.1f}%")

    return {
        "model": model,
        "scaler": scaler,
        "target_scaler": target_scaler,
        "history": history,
        "metrics": metrics,
        "artifact_paths": {
            "model": model_path,
            "scaler": scaler_file,
            "target_scaler": target_scaler_file,
            "meta": meta_path,
            "train_summary": str(train_summary_path),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train LSTM model for a target horizon")
    parser.add_argument("--app-name", type=str, default="test-app")
    parser.add_argument("--namespace", type=str, default="default")
    parser.add_argument("--csv", type=str, default="data/training-data/training_data_v2.csv")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument(
        "--target",
        type=str,
        default=DEFAULT_TARGET,
        choices=TARGET_COLUMNS,
        help="Target column to predict",
    )
    parser.add_argument(
        "--test-split",
        type=float,
        default=0.1,
        help="Fraction of data to hold out for testing (default: 0.1)",
    )
    parser.add_argument("--output-dir", type=str, default="model/artifacts")
    parser.add_argument(
        "--target-floor",
        type=float,
        default=5.0,
        help="Minimum target RPS for rps_* targets to reduce near-zero noise",
    )
    parser.add_argument("--patience", type=int, default=15, help="Early stopping patience")
    args = parser.parse_args()

    result = train_model(
        csv_path=args.csv,
        lookback=args.lookback,
        epochs=args.epochs,
        target_col=args.target,
        app_name=args.app_name,
        namespace=args.namespace,
        test_split=args.test_split,
        output_dir=args.output_dir,
        target_floor=args.target_floor,
        early_stopping_patience=args.patience,
    )
    if result:
        print(f"\nTraining complete. Val MAE: {result['metrics']['val_mae']:.4f}")
    else:
        sys.exit(1)
