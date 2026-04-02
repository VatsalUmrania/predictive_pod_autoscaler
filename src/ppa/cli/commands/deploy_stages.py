"""Deploy command stage implementations.

Contains stages for PPA operator deployment (retrain → convert → build → deploy).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from ppa.cli.utils import error, info, run_cmd, step_heading, success, warn
from ppa.config import (
    CHAMPION_DIR,
    DEFAULT_NAMESPACE,
    DEPLOY_DIR,
)

if TYPE_CHECKING:
    from rich.progress import Progress, TaskID


def retrain_lstm(
    progress: Progress,
    task: TaskID,
    current: int,
    total_steps: int,
    app_name: str,
    csv: str,
    horizon: str,
    lookback: int,
    epochs: int,
    patience: int,
    artifacts_dir: str,
) -> int:
    """Stage 1-3: Retrain, convert, and promote LSTM models."""
    # Step 1: Retrain
    current += 1
    step_heading(current, total_steps, f"Retraining LSTM ({horizon})")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Retraining LSTM")

    if not os.path.exists(csv):
        error(f"Training CSV not found: {csv}")
        raise typer.Exit(1)

    try:
        from ppa.model.train import train_model

        result = train_model(
            csv_path=csv,
            lookback=lookback,
            epochs=epochs,
            target_col=horizon,
            output_dir=artifacts_dir,
            early_stopping_patience=patience,
        )
        if result is None:
            error("Training failed")
            raise typer.Exit(1)
        success(f"Training complete — Val MAE: {result['metrics']['val_mae']:.4f}")
    except ImportError:
        warn("Cannot import ppa.model.train — falling back to subprocess")
        run_cmd(
            [
                "python",
                "-m",
                "ppa.model.train",
                "--csv",
                csv,
                "--lookback",
                str(lookback),
                "--epochs",
                str(epochs),
                "--target",
                horizon,
                "--output-dir",
                artifacts_dir,
                "--patience",
                str(patience),
            ],
            title="Training LSTM",
        )

    progress.advance(task)

    # Step 2: Convert
    current += 1
    step_heading(current, total_steps, "Convert → TFLite (with int8 quantization)")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Converting to TFLite")

    try:
        from ppa.model.convert import convert_model

        model_path = Path(artifacts_dir) / "model.keras"
        tflite_path = Path(artifacts_dir) / "model.tflite"

        if not model_path.exists():
            error(f"Keras model not found: {model_path}")
            raise typer.Exit(1)

        convert_model(
            model_path=str(model_path),
            output_path=str(tflite_path),
            quantize=True,
        )
        success(f"Converted to TFLite: {tflite_path}")
    except (ImportError, AttributeError, TypeError):
        warn("Cannot convert model directly — using subprocess")
        run_cmd(
            [
                "python",
                "-m",
                "ppa.model.convert",
                "--keras",
                str(Path(artifacts_dir) / "model.keras"),
                "--tflite",
                str(Path(artifacts_dir) / "model.tflite"),
                "--quantize",
            ],
            title="Converting model to TFLite",
        )

    progress.advance(task)

    # Step 3: Promote
    current += 1
    step_heading(current, total_steps, "Promote artifacts → champion")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Promoting artifacts")

    champion_dir = CHAMPION_DIR / app_name / horizon
    champion_dir.mkdir(parents=True, exist_ok=True)

    for filename in ["model.tflite", "model.keras", "scaler.pkl"]:
        src = Path(artifacts_dir) / filename
        if src.exists():
            import shutil

            shutil.copy2(src, champion_dir / filename)
            success(f"Promoted {filename}")

    progress.advance(task)
    return current


def handle_hpa(
    progress: Progress,
    task: TaskID,
    current: int,
    total_steps: int,
    app_name: str,
    delete_hpa: bool | None,
) -> int:
    """Stage: Handle existing HPA and scale operator down."""
    current += 1
    step_heading(current, total_steps, "Handle HPA")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Handle HPA")

    if delete_hpa is not None:
        if delete_hpa:
            info(f"Deleting HPA for {app_name}")
            run_cmd(
                ["kubectl", "delete", "hpa", app_name, "-n", DEFAULT_NAMESPACE],
                title="Delete HPA",
            )
            success("HPA deleted")
        else:
            info(f"Keeping HPA for {app_name}")
            success("HPA kept")
    else:
        info("HPA handling skipped (use --delete-hpa or --keep-hpa to control)")

    progress.advance(task)

    # Scale operator down
    current += 1
    step_heading(current, total_steps, "Scale operator deployment to 0")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Scale operator")

    run_cmd(
        ["kubectl", "scale", "deployment", "ppa-operator", "--replicas=0", "-n", DEFAULT_NAMESPACE],
        title="Scale operator → 0",
    )
    success("Operator scaled down")
    progress.advance(task)

    return current


def build_and_deploy(
    progress: Progress,
    task: TaskID,
    current: int,
    total_steps: int,
    skip_build: bool,
    lookback: int,
) -> int:
    """Stage: Build Docker image, push models, deploy operator, apply CR."""
    # Build (conditional)
    if not skip_build:
        current += 1
        step_heading(current, total_steps, "Build Docker image")
        progress.update(task, description=f"[bold]Step {current}:[/bold] Build Docker")

        run_cmd(
            ["docker", "build", "-t", "ppa-operator:latest", str(DEPLOY_DIR)],
            title="Building operator image",
        )
        success("Built ppa-operator:latest")
        progress.advance(task)
    else:
        info("Skipping Docker build (use --skip-build)")

    # Push to registry
    current += 1
    step_heading(current, total_steps, "Push operator image")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Push image")

    info("Registry push skipped (configure DOCKER_REGISTRY for image push)")
    progress.advance(task)

    # Deploy operator
    current += 1
    step_heading(current, total_steps, "Deploy operator")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Deploy operator")

    run_cmd(
        ["kubectl", "apply", "-f", str(DEPLOY_DIR / "operator.yaml")],
        title="Deploying operator",
    )
    success("Operator deployed")
    progress.advance(task)

    # Apply CR
    current += 1
    step_heading(current, total_steps, "Apply PredictiveAutoscaler CR")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Apply CR")

    cr_file = DEPLOY_DIR / "predictive-autoscaler-cr.yaml"
    if cr_file.exists():
        run_cmd(
            ["kubectl", "apply", "-f", str(cr_file)],
            title="Applying CR",
        )
        success("PredictiveAutoscaler CR applied")
    else:
        warn(f"CR file not found: {cr_file}")

    progress.advance(task)
    return current


def tail_logs(no_watch: bool) -> None:
    """Tail operator logs."""
    if no_watch:
        info("Skipping log watch (use --watch to tail)")
        return

    info("Streaming operator logs (Press Ctrl+C to stop)...")
    try:
        run_cmd(
            ["kubectl", "logs", "-f", "-l", "app=ppa-operator", "-n", DEFAULT_NAMESPACE],
            title="Streaming logs",
        )
    except KeyboardInterrupt:
        info("Log watch stopped")
