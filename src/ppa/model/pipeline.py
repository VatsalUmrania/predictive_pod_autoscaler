# model/pipeline.py — End-to-end ML pipeline orchestrator
"""Train → Evaluate → Convert for one or more prediction horizons.

Usage:
    python model/pipeline.py --csv data/training-data/training_data_v2.csv
    python model/pipeline.py --csv ... --horizons rps_t3m,rps_t10m --epochs 20
    python model/pipeline.py --csv ... --quality-gate 30
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ppa.common.feature_spec import TARGET_COLUMNS
from ppa.model.convert import convert_model
from ppa.model.deployment import patch_predictiveautoscaler_paths
from ppa.model.evaluate import evaluate_model
from ppa.model.model_qualifier import load_json as _load_json, should_promote
from ppa.model.train import LOOKBACK_STEPS, train_model

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


# These functions are now in model_qualifier.py and deployment.py


def promote_artifacts(
    app_name: str,
    target: str,
    challenger_paths: dict,
    eval_summary_path: str,
    champion_dir: str,
) -> dict:
    """Copy winning challenger artifacts into champion/<app_name>/<target>/ canonical names."""
    target_dir = os.path.join(champion_dir, app_name, target)
    os.makedirs(target_dir, exist_ok=True)

    dst_model = os.path.join(target_dir, "ppa_model.tflite")
    dst_scaler = os.path.join(target_dir, "scaler.pkl")
    dst_target_scaler = os.path.join(target_dir, "target_scaler.pkl")
    dst_summary = os.path.join(target_dir, "eval_summary.json")

    shutil.copy2(challenger_paths["tflite"], dst_model)
    shutil.copy2(challenger_paths["scaler"], dst_scaler)
    if challenger_paths.get("target_scaler") and os.path.exists(challenger_paths["target_scaler"]):
        shutil.copy2(challenger_paths["target_scaler"], dst_target_scaler)
    shutil.copy2(eval_summary_path, dst_summary)

    # Copy evaluation plots produced by evaluate.py into the champion folder
    artifacts_dir = os.path.dirname(eval_summary_path)
    for plot_name in [
        f"eval_pred_vs_actual_{target}.png",
        f"eval_ppa_vs_hpa_{target}.png",
    ]:
        src_plot = os.path.join(artifacts_dir, plot_name)
        if os.path.exists(src_plot):
            shutil.copy2(src_plot, os.path.join(target_dir, plot_name))

    return {
        "model": dst_model,
        "scaler": dst_scaler,
        "target_scaler": (dst_target_scaler if os.path.exists(dst_target_scaler) else None),
        "summary": dst_summary,
    }



# patch_predictiveautoscaler_paths is now in deployment.py

def run_pipeline(
    app_name: str,
    csv_path: str,
    horizons: list[str],
    epochs: int = 50,
    output_dir: str = "model/artifacts",
    quality_gate: float = 25.0,
    gate_metric: str = "smape",
    low_traffic_threshold: float = 10.0,
    test_split: float = 0.1,
    lookback: int = LOOKBACK_STEPS,
    quantize: bool = True,
    target_floor: float = 5.0,
    patience: int = 15,
    promote_if_better: bool = False,
    champion_dir: str | None = None,
    promotion_metric: str = "smape",
    promotion_gate: float = 35.0,
    min_relative_improvement: float = 0.02,
    max_underprov_regression: float = 1.0,
    promote_cr_name: str | None = None,
    promote_cr_namespace: str | None = None,
) -> int:
    """Run the full pipeline for each horizon. Returns exit code (0=all pass)."""
    results = []

    for target in horizons:
        print(f"\n{'=' * 60}")
        print(f"  PIPELINE — {target} ({app_name})")
        print(f"{'=' * 60}")

        # Train and evaluate model
        result_dict = _train_and_evaluate_model(
            app_name=app_name,
            target=target,
            csv_path=csv_path,
            epochs=epochs,
            output_dir=output_dir,
            lookback=lookback,
            test_split=test_split,
            target_floor=target_floor,
            patience=patience,
            low_traffic_threshold=low_traffic_threshold,
            quality_gate=quality_gate,
            gate_metric=gate_metric,
            quantize=quantize,
        )

        if result_dict is None:
            results.append({
                "target": target,
                "status": "TRAIN_FAILED",
                "mape": None,
                "mae": None,
                "rmse": None,
                "size_kb": None,
                "passed": False,
            })
            continue

        # Check promotion if enabled
        if promote_if_better:
            promotion_decision, promotion_reason = _handle_promotion(
                app_name=app_name,
                target=target,
                result_dict=result_dict,
                champion_dir=champion_dir,
                promotion_metric=promotion_metric,
                promotion_gate=promotion_gate,
                min_relative_improvement=min_relative_improvement,
                max_underprov_regression=max_underprov_regression,
                promote_cr_name=promote_cr_name,
                promote_cr_namespace=promote_cr_namespace,
            )
        else:
            promotion_decision = None
            promotion_reason = None

        results.append(result_dict | {
            "promotion": promotion_decision,
            "promotion_reason": promotion_reason,
        })

    # Print summary
    _print_pipeline_summary(results)

    return 1 if any(not r["passed"] for r in results) else 0


def _train_and_evaluate_model(
    app_name: str,
    target: str,
    csv_path: str,
    epochs: int,
    output_dir: str,
    lookback: int,
    test_split: float,
    target_floor: float,
    patience: int,
    low_traffic_threshold: float,
    quality_gate: float,
    gate_metric: str,
    quantize: bool,
) -> dict | None:
    """Train, evaluate, and convert model. Returns result dict or None on failure."""
    # Stage 1: Train
    train_result = train_model(
        csv_path=csv_path,
        lookback=lookback,
        epochs=epochs,
        target_col=target,
        test_split=test_split,
        output_dir=output_dir,
        target_floor=target_floor,
        early_stopping_patience=patience,
    )

    if train_result is None:
        return None

    paths = train_result["artifact_paths"]

    # Stage 2: Evaluate
    eval_result = evaluate_model(
        model_path=paths["model"],
        scaler_path=paths["scaler"],
        csv_path=csv_path,
        target_col=target,
        output_dir=output_dir,
        lookback=lookback,
        test_split=test_split,
        meta_path=paths["meta"],
        target_scaler_path=paths.get("target_scaler"),
        low_traffic_threshold=low_traffic_threshold,
    )

    if eval_result is None:
        return {
            "target": target,
            "status": "EVAL_FAILED",
            "mape": None,
            "mae": None,
            "rmse": None,
            "size_kb": None,
            "passed": False,
        }

    gate_value = eval_result.get(gate_metric, eval_result["mape"])
    passed = gate_value <= quality_gate

    # Stage 3: Convert
    tflite_path = os.path.join(output_dir, f"ppa_model_{target}.tflite")
    conv_result = convert_model(
        model_path=paths["model"],
        quantize=quantize,
        output_path=tflite_path,
    )

    size_kb = conv_result["size_kb"] if conv_result else None
    status = "PASS" if passed else f"WARN_{gate_metric.upper()}"

    if not passed:
        print(f"\n  ⚠  Quality gate: {gate_metric.upper()} {gate_value:.2f}% > {quality_gate}% threshold")

    return {
        "target": target,
        "status": status,
        "mape": eval_result["mape"],
        "smape": eval_result.get("smape"),
        "mape_filtered": eval_result.get("mape_filtered"),
        "gate_metric": gate_metric,
        "gate_value": gate_value,
        "mae": eval_result["mae"],
        "rmse": eval_result["rmse"],
        "size_kb": size_kb,
        "passed": passed,
        "eval_result": eval_result,
        "paths": paths,
        "tflite_path": tflite_path,
    }


def _handle_promotion(
    app_name: str,
    target: str,
    result_dict: dict,
    champion_dir: str | None,
    promotion_metric: str,
    promotion_gate: float,
    min_relative_improvement: float,
    max_underprov_regression: float,
    promote_cr_name: str | None,
    promote_cr_namespace: str | None,
) -> tuple[str | None, str | None]:
    """Handle model promotion if enabled. Returns (promotion_decision, promotion_reason)."""
    if not champion_dir:
        return "HOLD", "champion_dir not provided"

    eval_result = result_dict["eval_result"]
    paths = result_dict["paths"]
    tflite_path = result_dict["tflite_path"]

    champion_summary_path = os.path.join(champion_dir, app_name, target, "eval_summary.json")
    champion_metrics = _load_json(champion_summary_path)

    promote, reason = should_promote(
        champion_metrics=champion_metrics,
        challenger_metrics=eval_result,
        metric=promotion_metric,
        gate_threshold=promotion_gate,
        min_relative_improvement=min_relative_improvement,
        max_underprov_regression=max_underprov_regression,
    )

    if not promote:
        print(f"  ⏸  Champion hold ({target}): {reason}")
        return "HOLD", reason

    # Promotion approved, apply it
    promoted_paths = promote_artifacts(
        app_name=app_name,
        target=target,
        challenger_paths={
            "tflite": tflite_path,
            "scaler": paths["scaler"],
            "target_scaler": paths.get("target_scaler"),
        },
        eval_summary_path=os.path.join(os.path.dirname(tflite_path), f"eval_summary_{target}.json"),
        champion_dir=champion_dir,
    )

    print(f"  ✅ Champion update ({target}): {reason}")
    print(f"     model={promoted_paths['model']}")

    # Patch CR if requested
    if promote_cr_name and promote_cr_namespace:
        patched, msg = patch_predictiveautoscaler_paths(
            cr_name=promote_cr_name,
            cr_namespace=promote_cr_namespace,
            model_path=promoted_paths["model"],
            scaler_path=promoted_paths["scaler"],
            target_scaler_path=promoted_paths["target_scaler"],
        )
        if patched:
            print(f"     CR patched: {msg}")
            return "PROMOTED", reason
        else:
            print(f"     ⚠ CR patch failed: {msg}")
            return "PROMOTED_LOCAL_ONLY", f"{reason}; CR patch failed: {msg}"

    return "PROMOTED", reason


def _print_pipeline_summary(results: list[dict]) -> None:
    """Print formatted pipeline summary table."""
    print(f"\n\n{'=' * 72}")
    print("  PIPELINE SUMMARY")
    print(f"{'=' * 72}")
    print(
        f"  {'Target':<14} {'Status':<14} {'MAPE %':>8} {'sMAPE %':>8} {'Gate %':>8} {'MAE':>10} {'RMSE':>10} {'Size KB':>10} {'Promotion':>12}"
    )
    print(f"  {'─' * 66}")

    for r in results:
        mape_s = f"{r['mape']:.2f}" if r["mape"] is not None else "N/A"
        smape_s = f"{r['smape']:.2f}" if r.get("smape") is not None else "N/A"
        gate_s = f"{r['gate_value']:.2f}" if r.get("gate_value") is not None else "N/A"
        mae_s = f"{r['mae']:.4f}" if r["mae"] is not None else "N/A"
        rmse_s = f"{r['rmse']:.4f}" if r["rmse"] is not None else "N/A"
        size_s = f"{r['size_kb']:.1f}" if r["size_kb"] is not None else "N/A"
        promo_s = r.get("promotion") or "N/A"
        print(
            f"  {r['target']:<14} {r['status']:<14} {mape_s:>8} {smape_s:>8} {gate_s:>8} {mae_s:>10} {rmse_s:>10} {size_s:>10} {promo_s:>12}"
        )

    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run full ML pipeline (train → evaluate → convert)"
    )
    parser.add_argument(
        "--app-name",
        type=str,
        default="test-app",
        help="Target application name (default: test-app)",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="data/training-data/training_data_v2.csv",
        help="Path to training CSV",
    )
    parser.add_argument(
        "--horizons",
        type=str,
        default="rps_t3m,rps_t5m,rps_t10m",
        help="Comma-separated target columns to train",
    )
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--output-dir", type=str, default="data/artifacts")
    parser.add_argument(
        "--quality-gate",
        type=float,
        default=25.0,
        help="Gate threshold (%%); metric chosen by --gate-metric",
    )
    parser.add_argument(
        "--gate-metric",
        type=str,
        default="smape",
        choices=["mape", "smape", "mape_filtered"],
        help="Metric used for quality gate",
    )
    parser.add_argument(
        "--low-traffic-threshold",
        type=float,
        default=10.0,
        help="For mape_filtered, ignore rows with actual RPS <= this threshold",
    )
    parser.add_argument("--test-split", type=float, default=0.1)
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument(
        "--target-floor",
        type=float,
        default=5.0,
        help="Minimum target RPS floor for rps_* targets during training",
    )
    parser.add_argument("--patience", type=int, default=15, help="Early stopping patience")
    parser.add_argument("--no-quantize", action="store_true")
    parser.add_argument(
        "--promote-if-better",
        action="store_true",
        help="Promote challenger artifacts into champion_dir only if policy passes",
    )
    parser.add_argument(
        "--champion-dir",
        type=str,
        default=None,
        help="Directory storing active champion artifacts under <dir>/<app_name>/<target>/",
    )
    parser.add_argument(
        "--promotion-metric",
        type=str,
        default="smape",
        choices=["mape", "smape", "mape_filtered"],
        help="Metric used for champion-challenger comparison",
    )
    parser.add_argument(
        "--promotion-gate",
        type=float,
        default=35.0,
        help="Challenger must be <= this metric value to be eligible",
    )
    parser.add_argument(
        "--min-relative-improvement",
        type=float,
        default=0.02,
        help="Required relative improvement vs champion (e.g., 0.02 = 2%%)",
    )
    parser.add_argument(
        "--max-underprov-regression",
        type=float,
        default=1.0,
        help="Max allowed increase in ppa_under_prov_pct (absolute %% points)",
    )
    parser.add_argument(
        "--promote-cr-name",
        type=str,
        default=None,
        help="If set with --promote-cr-namespace, patch this PredictiveAutoscaler after promotion",
    )
    parser.add_argument(
        "--promote-cr-namespace",
        type=str,
        default=None,
        help="Namespace of PredictiveAutoscaler to patch after promotion",
    )
    args = parser.parse_args()

    horizons = [h.strip() for h in args.horizons.split(",")]
    for h in horizons:
        if h not in TARGET_COLUMNS:
            print(f"Error: '{h}' is not a valid target. Choose from {TARGET_COLUMNS}")
            sys.exit(1)

    exit_code = run_pipeline(
        app_name=args.app_name,
        csv_path=args.csv,
        horizons=horizons,
        epochs=args.epochs,
        output_dir=args.output_dir,
        quality_gate=args.quality_gate,
        gate_metric=args.gate_metric,
        low_traffic_threshold=args.low_traffic_threshold,
        test_split=args.test_split,
        lookback=args.lookback,
        quantize=not args.no_quantize,
        target_floor=args.target_floor,
        patience=args.patience,
        promote_if_better=args.promote_if_better,
        champion_dir=args.champion_dir,
        promotion_metric=args.promotion_metric,
        promotion_gate=args.promotion_gate,
        min_relative_improvement=args.min_relative_improvement,
        max_underprov_regression=args.max_underprov_regression,
        promote_cr_name=args.promote_cr_name,
        promote_cr_namespace=args.promote_cr_namespace,
    )
    sys.exit(exit_code)
