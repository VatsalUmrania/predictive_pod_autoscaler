# model/pipeline.py — End-to-end ML pipeline orchestrator
"""Train → Evaluate → Convert for one or more prediction horizons.

Usage:
    python model/pipeline.py --csv data-collection/training-data/training_data_v2.csv
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

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.feature_spec import TARGET_COLUMNS
from model.train import train_model, LOOKBACK_STEPS
from model.evaluate import evaluate_model
from model.convert import convert_model


def _load_json(path: str) -> dict | None:
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def should_promote(
    champion_metrics: dict | None,
    challenger_metrics: dict,
    metric: str = "smape",
    gate_threshold: float = 35.0,
    min_relative_improvement: float = 0.02,
    max_underprov_regression: float = 1.0,
) -> tuple[bool, str]:
    """Decide if challenger should replace champion.

    Rules:
      1) challenger metric must pass gate_threshold
      2) if no champion exists -> promote (bootstrap)
      3) challenger must improve metric by min_relative_improvement
      4) challenger must not worsen ppa_under_prov_pct beyond max_underprov_regression
    """
    challenger_metric = float(challenger_metrics.get(metric, float("inf")))
    if challenger_metric > gate_threshold:
        return False, f"challenger failed gate: {metric}={challenger_metric:.2f} > {gate_threshold:.2f}"

    if champion_metrics is None:
        return True, "no champion found (bootstrap promotion)"

    champion_metric = float(champion_metrics.get(metric, float("inf")))
    rel_improve = (champion_metric - challenger_metric) / max(abs(champion_metric), 1e-9)
    if rel_improve < min_relative_improvement:
        return False, (
            f"insufficient improvement: {metric} {champion_metric:.2f} -> {challenger_metric:.2f} "
            f"({rel_improve * 100:.2f}% < {min_relative_improvement * 100:.2f}%)"
        )

    champion_under = float(champion_metrics.get("ppa_under_prov_pct", 0.0))
    challenger_under = float(challenger_metrics.get("ppa_under_prov_pct", 0.0))
    if (challenger_under - champion_under) > max_underprov_regression:
        return False, (
            f"under-provisioning regression too high: {champion_under:.2f}% -> {challenger_under:.2f}%"
        )

    return True, (
        f"better {metric}: {champion_metric:.2f} -> {challenger_metric:.2f} "
        f"and under-provisioning acceptable"
    )


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
        "target_scaler": dst_target_scaler if os.path.exists(dst_target_scaler) else None,
        "summary": dst_summary,
    }


def patch_predictiveautoscaler_paths(
    cr_name: str,
    cr_namespace: str,
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None,
) -> tuple[bool, str]:
    """Patch PredictiveAutoscaler spec paths via kubectl."""
    spec = {
        "modelPath": model_path,
        "scalerPath": scaler_path,
    }
    if target_scaler_path:
        spec["targetScalerPath"] = target_scaler_path

    patch_payload = json.dumps({"spec": spec})
    cmd = [
        "kubectl",
        "-n",
        cr_namespace,
        "patch",
        "predictiveautoscaler",
        cr_name,
        "--type",
        "merge",
        "-p",
        patch_payload,
    ]

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, result.stdout.strip() or "patched"
    except FileNotFoundError:
        return False, "kubectl not found"
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        return False, stderr or stdout or f"kubectl patch failed: code {exc.returncode}"


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
        print(f"\n{'='*60}")
        print(f"  PIPELINE — {target} ({app_name})")
        print(f"{'='*60}")

        # ── Stage 1: Train ──────────────────────────────────────────
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
            target_scaler_path=paths.get("target_scaler"),
            low_traffic_threshold=low_traffic_threshold,
        )

        if eval_result is None:
            results.append({
                "target": target,
                "status": "EVAL_FAILED",
                "mape": None, "mae": None, "rmse": None,
                "size_kb": None, "passed": False,
            })
            continue

        gate_value = eval_result.get(gate_metric, eval_result["mape"])
        passed = gate_value <= quality_gate

        # ── Stage 3: Convert ───────────────────────────────────────
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

        promotion_decision = None
        promotion_reason = None
        if promote_if_better:
            if not champion_dir:
                promotion_decision = "HOLD"
                promotion_reason = "champion_dir not provided"
            else:
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
                if promote and conv_result is not None:
                    promoted_paths = promote_artifacts(
                        target=target,
                        challenger_paths={
                            "tflite": tflite_path,
                            "scaler": paths["scaler"],
                            "target_scaler": paths.get("target_scaler"),
                        },
                        eval_summary_path=os.path.join(output_dir, f"eval_summary_{target}.json"),
                        champion_dir=champion_dir,
                    )
                    promotion_decision = "PROMOTED"
                    promotion_reason = reason
                    print(f"  ✅ Champion update ({target}): {reason}")
                    print(f"     model={promoted_paths['model']}")

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
                        else:
                            promotion_decision = "PROMOTED_LOCAL_ONLY"
                            promotion_reason = f"{reason}; CR patch failed: {msg}"
                            print(f"     ⚠ CR patch failed: {msg}")
                else:
                    promotion_decision = "HOLD"
                    promotion_reason = reason if conv_result is not None else "conversion failed, cannot promote"
                    print(f"  ⏸  Champion hold ({target}): {promotion_reason}")

        results.append({
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
            "promotion": promotion_decision,
            "promotion_reason": promotion_reason,
        })

    # ── Final summary ───────────────────────────────────────────────
    print(f"\n\n{'='*72}")
    print("  PIPELINE SUMMARY")
    print(f"{'='*72}")
    print(f"  {'Target':<14} {'Status':<14} {'MAPE %':>8} {'sMAPE %':>8} {'Gate %':>8} {'MAE':>10} {'RMSE':>10} {'Size KB':>10} {'Promotion':>12}")
    print(f"  {'─'*66}")

    any_failed = False
    for r in results:
        mape_s = f"{r['mape']:.2f}" if r["mape"] is not None else "N/A"
        smape_s = f"{r['smape']:.2f}" if r.get("smape") is not None else "N/A"
        gate_s = f"{r['gate_value']:.2f}" if r.get("gate_value") is not None else "N/A"
        mae_s = f"{r['mae']:.4f}" if r["mae"] is not None else "N/A"
        rmse_s = f"{r['rmse']:.4f}" if r["rmse"] is not None else "N/A"
        size_s = f"{r['size_kb']:.1f}" if r["size_kb"] is not None else "N/A"
        promo_s = r.get("promotion") or "N/A"
        print(f"  {r['target']:<14} {r['status']:<14} {mape_s:>8} {smape_s:>8} {gate_s:>8} {mae_s:>10} {rmse_s:>10} {size_s:>10} {promo_s:>12}")
        if not r["passed"]:
            any_failed = True

    print(f"{'='*72}\n")

    return 1 if any_failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run full ML pipeline (train → evaluate → convert)")
    parser.add_argument("--app-name", type=str, default="test-app", help="Target application name (default: test-app)")
    parser.add_argument("--csv", type=str, default="data-collection/training-data/training_data_v2.csv",
                        help="Path to training CSV")
    parser.add_argument("--horizons", type=str, default="rps_t3m,rps_t5m,rps_t10m",
                        help="Comma-separated target columns to train")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--output-dir", type=str, default="model/artifacts")
    parser.add_argument("--quality-gate", type=float, default=25.0,
                        help="Gate threshold (%%); metric chosen by --gate-metric")
    parser.add_argument("--gate-metric", type=str, default="smape", choices=["mape", "smape", "mape_filtered"],
                        help="Metric used for quality gate")
    parser.add_argument("--low-traffic-threshold", type=float, default=10.0,
                        help="For mape_filtered, ignore rows with actual RPS <= this threshold")
    parser.add_argument("--test-split", type=float, default=0.1)
    parser.add_argument("--lookback", type=int, default=LOOKBACK_STEPS)
    parser.add_argument("--target-floor", type=float, default=5.0,
                        help="Minimum target RPS floor for rps_* targets during training")
    parser.add_argument("--patience", type=int, default=15,
                        help="Early stopping patience")
    parser.add_argument("--no-quantize", action="store_true")
    parser.add_argument("--promote-if-better", action="store_true",
                        help="Promote challenger artifacts into champion_dir only if policy passes")
    parser.add_argument("--champion-dir", type=str, default=None,
                        help="Directory storing active champion artifacts under <dir>/<app_name>/<target>/")
    parser.add_argument("--promotion-metric", type=str, default="smape",
                        choices=["mape", "smape", "mape_filtered"],
                        help="Metric used for champion-challenger comparison")
    parser.add_argument("--promotion-gate", type=float, default=35.0,
                        help="Challenger must be <= this metric value to be eligible")
    parser.add_argument("--min-relative-improvement", type=float, default=0.02,
                        help="Required relative improvement vs champion (e.g., 0.02 = 2%%)")
    parser.add_argument("--max-underprov-regression", type=float, default=1.0,
                        help="Max allowed increase in ppa_under_prov_pct (absolute %% points)")
    parser.add_argument("--promote-cr-name", type=str, default=None,
                        help="If set with --promote-cr-namespace, patch this PredictiveAutoscaler after promotion")
    parser.add_argument("--promote-cr-namespace", type=str, default=None,
                        help="Namespace of PredictiveAutoscaler to patch after promotion")
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
