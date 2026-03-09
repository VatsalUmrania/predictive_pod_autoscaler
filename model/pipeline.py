# model/pipeline.py — End-to-end ML pipeline orchestrator
"""Train → Evaluate → Convert for one or more prediction horizons.

Usage:
    python model/pipeline.py --csv data-collection/training-data/training_data_v2.csv
    python model/pipeline.py --csv ... --horizons rps_t3m,rps_t10m --epochs 20
    python model/pipeline.py --csv ... --quality-gate 30
"""

import argparse
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import TARGET_COLUMNS
from model.train import train_model, LOOKBACK_STEPS
from model.evaluate import evaluate_model
from model.convert import convert_model


def run_pipeline(
    csv_path: str,
    horizons: list[str],
    epochs: int = 50,
    output_dir: str = "model/artifacts",
    quality_gate: float = 25.0,
    test_split: float = 0.1,
    lookback: int = LOOKBACK_STEPS,
    quantize: bool = True,
) -> int:
    """Run the full pipeline for each horizon. Returns exit code (0=all pass)."""
    results = []

    for target in horizons:
        print(f"\n{'='*60}")
        print(f"  PIPELINE — {target}")
        print(f"{'='*60}")

        # ── Stage 1: Train ──────────────────────────────────────────
        train_result = train_model(
            csv_path=csv_path,
            lookback=lookback,
            epochs=epochs,
            target_col=target,
            test_split=test_split,
            output_dir=output_dir,
        )

        if train_result is None:
            results.append({
                "target": target,
                "status": "TRAIN_FAILED",
                "mape": None, "mae": None, "rmse": None,
                "size_kb": None, "passed": False,
            })
            continue

        paths = train_result["artifact_paths"]

        # ── Stage 2: Evaluate ───────────────────────────────────────
        eval_result = evaluate_model(
            model_path=paths["model"],
            scaler_path=paths["scaler"],
            csv_path=csv_path,
            target_col=target,
            output_dir=output_dir,
            lookback=lookback,
            test_split=test_split,
            meta_path=paths["meta"],
        )

        if eval_result is None:
            results.append({
                "target": target,
                "status": "EVAL_FAILED",
                "mape": None, "mae": None, "rmse": None,
                "size_kb": None, "passed": False,
            })
            continue

        passed = eval_result["mape"] <= quality_gate

        # ── Stage 3: Convert ───────────────────────────────────────
        tflite_path = os.path.join(output_dir, f"ppa_model_{target}.tflite")
        conv_result = convert_model(
            model_path=paths["model"],
            quantize=quantize,
            output_path=tflite_path,
        )

        size_kb = conv_result["size_kb"] if conv_result else None

        status = "PASS" if passed else "WARN_MAPE"
        if not passed:
            print(f"\n  ⚠  Quality gate: MAPE {eval_result['mape']:.2f}% > {quality_gate}% threshold")

        results.append({
            "target": target,
            "status": status,
            "mape": eval_result["mape"],
            "mae": eval_result["mae"],
            "rmse": eval_result["rmse"],
            "size_kb": size_kb,
            "passed": passed,
        })

    # ── Final summary ───────────────────────────────────────────────
    print(f"\n\n{'='*72}")
    print("  PIPELINE SUMMARY")
    print(f"{'='*72}")
    print(f"  {'Target':<14} {'Status':<14} {'MAPE %':>8} {'MAE':>10} {'RMSE':>10} {'Size KB':>10}")
    print(f"  {'─'*66}")

    any_failed = False
    for r in results:
        mape_s = f"{r['mape']:.2f}" if r["mape"] is not None else "N/A"
        mae_s = f"{r['mae']:.4f}" if r["mae"] is not None else "N/A"
        rmse_s = f"{r['rmse']:.4f}" if r["rmse"] is not None else "N/A"
        size_s = f"{r['size_kb']:.1f}" if r["size_kb"] is not None else "N/A"
        print(f"  {r['target']:<14} {r['status']:<14} {mape_s:>8} {mae_s:>10} {rmse_s:>10} {size_s:>10}")
        if not r["passed"]:
            any_failed = True

    print(f"{'='*72}\n")

    return 1 if any_failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run full ML pipeline (train → evaluate → convert)")
    parser.add_argument("--csv", type=str, default="data-collection/training-data/training_data_v2.csv",
                        help="Path to training CSV")
    parser.add_argument("--horizons", type=str, default="rps_t3m,rps_t5m,rps_t10m",
                        help="Comma-separated target columns to train")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--output-dir", type=str, default="model/artifacts")
    parser.add_argument("--quality-gate", type=float, default=25.0,
                        help="MAPE threshold (%%); horizons above this are flagged WARN")
    parser.add_argument("--test-split", type=float, default=0.1)
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    horizons = [h.strip() for h in args.horizons.split(",")]
    for h in horizons:
        if h not in TARGET_COLUMNS:
            print(f"Error: '{h}' is not a valid target. Choose from {TARGET_COLUMNS}")
            sys.exit(1)

    exit_code = run_pipeline(
        csv_path=args.csv,
        horizons=horizons,
        epochs=args.epochs,
        output_dir=args.output_dir,
        quality_gate=args.quality_gate,
        test_split=args.test_split,
        lookback=args.lookback,
        quantize=not args.no_quantize,
    )
    sys.exit(exit_code)
