# model/evaluate.py — Model evaluation and HPA comparison
"""Evaluate LSTM predictions vs actual load, compare with HPA behavior.

Outputs:
    - MAPE, MAE, RMSE metrics
    - Predicted vs actual time series plot
    - PPA scaling timeline vs HPA scaling timeline
    - Side-by-side comparison table / JSON summary
"""

import argparse
import json
import os
import sys
from pathlib import Path

import joblib
import keras as _keras
import numpy as np
import pandas as pd
import tensorflow as tf

from ppa.common.constants import CAPACITY_PER_POD
from ppa.common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS
from ppa.model.train import LOOKBACK_STEPS, create_dataset_from_segments

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Backward-compat shim: models trained before the MSE migration used
# asymmetric_huber as their loss.  Registering it here allows Keras to
# deserialise those .keras files without error.  New models compiled with
# loss="mse" don't need this, but it is harmless to keep registered.


@_keras.saving.register_keras_serializable(package="ppa")
def asymmetric_huber(y_true, y_pred):  # noqa: F811
    error = y_true - y_pred
    weight = tf.where(error > 0, 3.0, 1.0)
    delta = 1.0
    abs_err = tf.abs(error)
    huber = tf.where(abs_err <= delta, 0.5 * tf.square(error), delta * (abs_err - 0.5 * delta))
    return tf.reduce_mean(weight * huber)


# ── Metric helpers ──────────────────────────────────────────────────────────


def compute_mape(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean Absolute Percentage Error (%), ignoring near-zero actuals."""
    mask = np.abs(y_true) > 1e-6
    if mask.sum() == 0:
        return 0.0
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def compute_smape(y_true: np.ndarray, y_pred: np.ndarray, min_denominator: float = 1.0) -> float:
    """Symmetric MAPE (%), robust when actual values are near zero."""
    denominator = (np.abs(y_true) + np.abs(y_pred)) / 2.0
    denominator = np.maximum(denominator, min_denominator)
    return float(np.mean(np.abs(y_true - y_pred) / denominator) * 100)


def compute_mape_filtered(
    y_true: np.ndarray, y_pred: np.ndarray, min_actual_rps: float = 10.0
) -> float:
    """MAPE (%) computed only for rows where actual load exceeds min_actual_rps."""
    mask = y_true > min_actual_rps
    if mask.sum() == 0:
        return 0.0
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def compute_mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.mean(np.abs(y_true - y_pred)))


def compute_rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


# ── HPA comparison helpers ──────────────────────────────────────────────────


def rps_to_replicas(rps: np.ndarray, capacity: float, min_r: int, max_r: int) -> np.ndarray:
    """Convert RPS values to replica counts (ceil division, clamped)."""
    replicas = np.ceil(rps / capacity).astype(int)
    return np.clip(replicas, min_r, max_r)  # type: ignore[no-any-return]


def compute_scaling_stats(
    actual_rps: np.ndarray,
    replica_counts: np.ndarray,
    capacity: float,
    label: str,
) -> dict:
    """Compute over/under-provisioning stats for a replica timeline."""
    provided_capacity = replica_counts * capacity
    over = provided_capacity - actual_rps
    under = actual_rps - provided_capacity

    over_prov_mask = over > 0
    under_prov_mask = under > 0

    return {
        f"{label}_avg_replicas": float(np.mean(replica_counts)),
        f"{label}_over_prov_pct": float(np.mean(over_prov_mask) * 100),
        f"{label}_under_prov_pct": float(np.mean(under_prov_mask) * 100),
        f"{label}_wasted_capacity_avg": float(np.mean(np.maximum(over, 0))),
    }


# ── Plotting ────────────────────────────────────────────────────────────────


def plot_pred_vs_actual(y_true, y_pred, target_col, output_path):
    """Generate predicted vs actual time-series plot."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(y_true, label="Actual", alpha=0.8, linewidth=0.8)
    ax.plot(y_pred, label="Predicted", alpha=0.8, linewidth=0.8)
    ax.set_title(f"Predicted vs Actual — {target_col}")
    ax.set_xlabel("Test sample index")
    ax.set_ylabel("RPS")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"  Plot saved → {output_path}")


def plot_ppa_vs_hpa(
    actual_rps,
    ppa_replicas,
    hpa_replicas,
    target_col,
    output_path,
):
    """Dual-axis plot: replica counts + RPS over time."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax1 = plt.subplots(figsize=(14, 5))

    color_rps = "tab:gray"
    ax1.set_xlabel("Test sample index")
    ax1.set_ylabel("RPS", color=color_rps)
    ax1.plot(actual_rps, color=color_rps, alpha=0.4, linewidth=0.6, label="Actual RPS")
    ax1.tick_params(axis="y", labelcolor=color_rps)

    ax2 = ax1.twinx()
    ax2.set_ylabel("Replicas")
    ax2.step(
        range(len(ppa_replicas)),
        ppa_replicas,
        where="mid",
        label="PPA replicas",
        alpha=0.8,
        linewidth=1.2,
        color="tab:blue",
    )
    ax2.step(
        range(len(hpa_replicas)),
        hpa_replicas,
        where="mid",
        label="HPA replicas",
        alpha=0.8,
        linewidth=1.2,
        color="tab:orange",
    )
    ax2.legend(loc="upper right")

    ax1.set_title(f"PPA vs HPA Scaling — {target_col}")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    print(f"  Plot saved → {output_path}")


# ── Main evaluation ────────────────────────────────────────────────────────


def evaluate_model(
    model_path: str,
    scaler_path: str,
    csv_path: str,
    target_col: str = TARGET_COLUMNS[0],
    output_dir: str = "data/artifacts",
    lookback: int = LOOKBACK_STEPS,
    test_split: float = 0.1,
    min_replicas: int = 2,
    max_replicas: int = 20,
    capacity: float = CAPACITY_PER_POD,
    meta_path: str | None = None,
    target_scaler_path: str | None = None,
    low_traffic_threshold: float = 10.0,
) -> dict | None:
    """Run full evaluation and return metrics dict.

    If *meta_path* is given, test-set boundaries are taken from split metadata
    produced by train.py.  Otherwise, *test_split* fraction is used.

    If *target_scaler_path* is given, model predictions (scaled [0,1]) are
    inverse-transformed back to raw RPS before computing metrics.
    """
    if not os.path.exists(model_path):
        print(f"Model not found: {model_path}")
        return None
    if not os.path.exists(scaler_path):
        print(f"Scaler not found: {scaler_path}")
        return None
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        return None

    print(f"\n{'─' * 60}")
    print(f"Evaluating: {target_col}")
    print(f"{'─' * 60}")

    # Load model + scaler
    model = _keras.models.load_model(model_path)
    scaler = joblib.load(scaler_path)

    # Load target scaler if available (model outputs scaled [0,1] targets)
    target_scaler = None
    if target_scaler_path and os.path.exists(target_scaler_path):
        target_scaler = joblib.load(target_scaler_path)

    # Rebuild test set
    df = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
    df = df.dropna(subset=FEATURE_COLUMNS + [target_col])

    x, y = create_dataset_from_segments(df, FEATURE_COLUMNS, target_col, scaler, lookback)

    # Determine test boundaries
    if meta_path and os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        test_start = meta["test_start_idx"]
    else:
        test_start = int(len(x) * (1 - test_split))

    x_test, y_test = x[test_start:], y[test_start:]

    if len(x_test) == 0:
        print("No test samples available.")
        return None

    print(f"  Test samples: {len(x_test)}")

    # Predict
    y_pred = model.predict(x_test, verbose=0).flatten()

    # Inverse-transform predictions only.  y_test is already raw RPS
    # (create_dataset_from_segments returns unscaled targets), but y_pred
    # comes from a model trained on MinMaxScaled targets [0,1].
    if target_scaler is not None:
        y_pred = target_scaler.inverse_transform(y_pred.reshape(-1, 1)).flatten()

    # Metrics
    mape = compute_mape(y_test, y_pred)
    smape = compute_smape(y_test, y_pred)
    mape_filtered = compute_mape_filtered(y_test, y_pred, min_actual_rps=low_traffic_threshold)
    mae = compute_mae(y_test, y_pred)
    rmse = compute_rmse(y_test, y_pred)

    print(f"  MAPE : {mape:.2f}%")
    print(f"  sMAPE: {smape:.2f}%")
    print(f"  fMAPE(>{low_traffic_threshold:.1f} RPS): {mape_filtered:.2f}%")
    print(f"  MAE  : {mae:.4f}")
    print(f"  RMSE : {rmse:.4f}")

    # Plots
    os.makedirs(output_dir, exist_ok=True)
    pred_plot_path = os.path.join(output_dir, f"eval_pred_vs_actual_{target_col}.png")
    plot_pred_vs_actual(y_test, y_pred, target_col, pred_plot_path)

    # PPA vs HPA comparison
    # PPA: uses predicted RPS (lookahead) to set replicas
    ppa_replicas = rps_to_replicas(y_pred, capacity, min_replicas, max_replicas)
    # HPA: reactive — uses actual RPS at time t (no lookahead)
    hpa_replicas = rps_to_replicas(y_test, capacity, min_replicas, max_replicas)

    ppa_stats = compute_scaling_stats(y_test, ppa_replicas, capacity, "ppa")
    hpa_stats = compute_scaling_stats(y_test, hpa_replicas, capacity, "hpa")

    hpa_plot_path = os.path.join(output_dir, f"eval_ppa_vs_hpa_{target_col}.png")
    plot_ppa_vs_hpa(y_test, ppa_replicas, hpa_replicas, target_col, hpa_plot_path)

    # Summary
    summary = {
        "target": target_col,
        "test_samples": len(x_test),
        "mape": round(mape, 4),
        "smape": round(smape, 4),
        "mape_filtered": round(mape_filtered, 4),
        "low_traffic_threshold": float(low_traffic_threshold),
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        **ppa_stats,
        **hpa_stats,
        "replica_savings_pct": round(
            (1 - ppa_stats["ppa_avg_replicas"] / max(hpa_stats["hpa_avg_replicas"], 1e-6)) * 100,
            2,
        ),
    }

    summary_path = os.path.join(output_dir, f"eval_summary_{target_col}.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary saved → {summary_path}")

    # Print comparison table
    print(f"\n  {'Metric':<30} {'PPA':>10} {'HPA':>10}")
    print(f"  {'─' * 50}")
    print(
        f"  {'Avg Replicas':<30} {ppa_stats['ppa_avg_replicas']:>10.2f} {hpa_stats['hpa_avg_replicas']:>10.2f}"
    )
    print(
        f"  {'Over-provisioned %':<30} {ppa_stats['ppa_over_prov_pct']:>10.1f} {hpa_stats['hpa_over_prov_pct']:>10.1f}"
    )
    print(
        f"  {'Under-provisioned %':<30} {ppa_stats['ppa_under_prov_pct']:>10.1f} {hpa_stats['hpa_under_prov_pct']:>10.1f}"
    )
    print(
        f"  {'Wasted capacity (avg RPS)':<30} {ppa_stats['ppa_wasted_capacity_avg']:>10.1f} {hpa_stats['hpa_wasted_capacity_avg']:>10.1f}"
    )
    print(f"  {'Replica savings':<30} {summary['replica_savings_pct']:>10.1f}%")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate trained LSTM model")
    parser.add_argument("--model", type=str, required=True, help="Path to .keras model")
    parser.add_argument("--scaler", type=str, required=True, help="Path to scaler .pkl")
    parser.add_argument("--csv", type=str, default="data/training-data/training_data_v2.csv")
    parser.add_argument("--target", type=str, default=TARGET_COLUMNS[0], choices=TARGET_COLUMNS)
    parser.add_argument("--output-dir", type=str, default="data/artifacts")
    parser.add_argument(
        "--meta", type=str, default=None, help="Path to split_meta JSON from train.py"
    )
    parser.add_argument(
        "--target-scaler", type=str, default=None, help="Path to target_scaler .pkl"
    )
    parser.add_argument("--test-split", type=float, default=0.1)
    parser.add_argument(
        "--low-traffic-threshold",
        type=float,
        default=10.0,
        help="Rows with actual RPS <= this are excluded from filtered MAPE",
    )
    args = parser.parse_args()

    result = evaluate_model(
        model_path=args.model,
        scaler_path=args.scaler,
        csv_path=args.csv,
        target_col=args.target,
        output_dir=args.output_dir,
        meta_path=args.meta,
        test_split=args.test_split,
        target_scaler_path=args.target_scaler,
        low_traffic_threshold=args.low_traffic_threshold,
    )
    if result:
        print(f"\nEvaluation complete. MAPE: {result['mape']:.2f}%")
    else:
        sys.exit(1)
