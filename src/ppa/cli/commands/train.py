"""ppa train — Train the LSTM model.

Top-level command (previously ppa model train). Delegates to
ppa.model.train.train_model() for business logic.
"""

from __future__ import annotations

import os
import time

import typer

from ppa.cli.core.progress import TrainingProgressManager
from ppa.cli.utils import (
    console,
    error_block,
    info,
    next_step,
    success,
)
from ppa.config import (
    DEFAULT_APP_NAME,
    DEFAULT_EPOCHS,
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
    DEFAULT_NAMESPACE,
    TRAINING_DATA_DIR,
)
# Default training horizons for multi-model training
DEFAULT_TRAIN_HORIZONS = ["rps_t3m", "rps_t5m", "rps_t10m"]

def _resolve_training_horizons(horizons: str | None) -> list[str]:
    """Parse comma-separated horizons or return defaults.
    Args:
        horizons: Comma-separated string or None for defaults
    Returns:
        List of horizon strings
    """
    if horizons is None:
        return DEFAULT_TRAIN_HORIZONS
    return [h.strip() for h in horizons.split(",")]


def _build_training_progress_callback(horizon: str):
    """Build a progress callback for a single horizon's training.
    Args:
        horizon: The prediction horizon being trained (e.g., 'rps_t5m')
    Returns:
        A progress tracking callback for the training process
    """
    return None

def train_cmd(
    app: str | None = typer.Option(
        None, "--app", "-a", help="App name (required if multiple apps registered)."
    ),
    horizon: str = typer.Option(
        DEFAULT_HORIZON,
        "--horizon",
        help="Prediction horizon(s): single target or comma-separated list (e.g., rps_t3m,rps_t5m,rps_t10m).",
    ),
    csv: str | None = typer.Option(
        None, "--csv", help="Path to training CSV (default: auto-export from Prometheus)."
    ),
    epochs: int = typer.Option(DEFAULT_EPOCHS, "--epochs", help="Training epochs."),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, "--lookback", help="Lookback window steps."),
    patience: int = typer.Option(20, "--patience", help="Early stopping patience."),
    eval_only: bool = typer.Option(
        False, "--eval", help="Show evaluation metrics after training, skip deploy."
    ),
    apply_after: bool = typer.Option(
        False, "--apply", help="Run ppa apply automatically after training."
    ),
    no_progress: bool = typer.Option(
        False, "--no-progress", help="Disable progress indicators (for scripting)."
    ),
) -> None:
    """Train LSTM model(s) on Prometheus metrics.

    Supports single or multiple prediction horizons. When multiple horizons
    are specified, models are trained sequentially with progress tracking.
    By default, trains all models: rps_t3m, rps_t5m, and rps_t10m.

    \b
    EXAMPLES
      ppa train                             # train with all defaults
      ppa train --horizon rps_t5m,rps_t10m  # train 2 models
      ppa train --epochs 100 --eval         # train and inspect quality
      ppa train --apply                     # train then deploy
      ppa train --csv ./data/metrics.csv    # use local data file
      ppa train --no-progress               # disable progress (for CI)

    \b
    REQUIRES
      • ppa init must have been run
      • At least one app registered via ppa add
      • Prometheus reachable — check: ppa status --infra
      • Training data available — check: ppa data validate
    """
    app_name = app or DEFAULT_APP_NAME
    csv_path = csv or str(TRAINING_DATA_DIR / "training_data_v2.csv")

    if not os.path.exists(csv_path):
        error_block(
            "Training data not found",
            cause=f"File does not exist: {csv_path}",
            fix=f"ppa data export --app-name {app_name}",
            Expected=csv_path,
            Got="file not found",
        )
        raise typer.Exit(1)

    # Parse horizons: accept comma-separated list or use defaults
    horizons = _resolve_training_horizons(horizon) if horizon else _resolve_training_horizons(None)
    num_horizons = len(horizons)

    try:
        from ppa.model.train import train_model

        start_time = time.time()
        results = []  # Store results for each horizon

        # Train each horizon sequentially
        for idx, target_horizon in enumerate(horizons):
            current_num = idx + 1
            model_label = f"[{current_num}/{num_horizons}] {target_horizon}"

            # Initialize progress manager for this model
            progress_manager = TrainingProgressManager(
                total_epochs=epochs,
                use_colors=not no_progress,
                target_col=target_horizon,
                app_name=app_name,
                lookback=lookback,
            ) if not no_progress else None

            # Get callbacks if progress is enabled
            callbacks_kwargs = {}
            if progress_manager:
                callbacks_kwargs = progress_manager.get_callbacks()
            else:
                console.print(f"  [step]{current_num}.[/]  {model_label}")

            # Train model with callbacks (no output suppression - let train_model print init info)
            result = train_model(
                csv_path=csv_path,
                lookback=lookback,
                epochs=epochs,
                target_col=target_horizon,
                app_name=app_name,
                namespace=DEFAULT_NAMESPACE,
                test_split=0.1,
                target_floor=5.0,
                early_stopping_patience=patience,
                verbose=0,  # Suppress all Keras output when using progress
                **callbacks_kwargs,
            )

            # Ensure progress manager stops before results display
            if progress_manager:
                progress_manager.stop()


            if result is None:
                error_block(
                    "Training failed",
                    cause="train_model() returned None",
                    fix=f"ppa data validate --app {app_name}",
                )
                raise typer.Exit(1)

            results.append(result)

            # Print results table for this horizon
            if progress_manager and not no_progress:
                metrics = result.get("metrics", {})
                progress_manager.print_results_table(
                    test_loss=metrics.get("test_loss"),
                    test_mae=metrics.get("test_mae"),
                    test_mape=metrics.get("mape"),
                )

            console.print()

        duration = time.time() - start_time
        duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"

        # Show results for each horizon
        console.print()
        success("Training complete")
        console.print()
        console.print("  [info]Version[/]       lstm")
        console.print(f"  [info]Models[/]        {num_horizons} horizon(s)")
        console.print(f"  [info]Duration[/]      {duration_str}")

        # Show summary for first result (or all if multiple)
        if results:
            result = results[0]
            metrics = result.get("metrics", {})
            val_metrics = metrics.get("val", {})
            data_info = metrics.get("data", {})
            epochs_run = metrics.get("epochs_run", epochs)

            console.print()
            console.print("[bold]Training Results[/]")

            # Show primary metric (validation MAPE)
            if val_metrics.get("mape") is not None:
                console.print()
                console.print("[bold]Primary Metric (Validation)[/]")
                console.print(f"  MAPE            {val_metrics['mape']:>7.2f}%")
            else:
                console.print()
                console.print("[error]✗ MAPE not available[/]")

            # Show all validation metrics
            if val_metrics:
                console.print()
                console.print("[dim]Validation[/]")
                for key in ["mape", "smape", "mae", "rmse"]:
                    if key in val_metrics:
                        val = val_metrics[key]
                        suffix = "%" if key in ["mape", "smape"] else ""
                        console.print(f"  {key.upper():<15} {val:>10.4f}{suffix}")

            # Show dataset context
            if data_info:
                console.print()
                console.print("[dim]Data[/]")
                console.print(f"  Train           {data_info['train_size']:>7,}")
                console.print(f"  Val             {data_info['val_size']:>7,}")
                console.print(f"  Test            {data_info['test_size']:>7,}")

            # Show epochs and model path
            console.print()
            console.print("[dim]Training[/]")
            console.print(f"  Epochs          {epochs_run}/{epochs}")

            paths = result.get("artifact_paths", {})
            if paths:
                model_path = paths.get("keras_model", paths.get("model", ""))
                if model_path:
                    console.print(f"  Saved           {model_path}")

            # Show next step
            console.print()
            console.print("[info]Next step[/]")
            console.print("  ppa evaluate --model <path> --target <target>")
            console.print("  (to test on held-out set and check for overfitting)")

            # Show detailed metrics table
            # Reconstruct progress manager for summary display
            temp_pm = TrainingProgressManager(total_epochs=epochs_run)
            temp_pm.epoch_metrics = []
            for ep in range(1, epochs_run + 1):
                # Extract from history if available
                if hasattr(result.get("history"), "history"):
                    hist = result["history"].history
                    temp_pm.epoch_metrics.append(
                        {
                            "epoch": ep,
                            "loss": hist.get("loss", [0])[min(ep - 1, len(hist.get("loss", [])) - 1)],
                            "val_loss": hist.get("val_loss", [0])[
                                min(ep - 1, len(hist.get("val_loss", [])) - 1)
                            ],
                            "mae": hist.get("mae", [0])[min(ep - 1, len(hist.get("mae", [])) - 1)],
                        }
                    )

            summary = temp_pm.summary_table()
            if summary:
                console.print()
                console.print(summary)

        # Handle --eval (show metrics, skip deploy)
        if eval_only:
            console.print()
            info("Evaluation mode — deployment skipped.")
            next_step(
                f"ppa apply --app-name {app_name}",
                "deploy the trained model",
            )
            raise typer.Exit()

        # Handle --apply (chain into apply)
        if apply_after:
            console.print()
            console.print(f"  [info]Deploying[/]  [bold]lstm[/]  →  {app_name}")
            console.print()

            from ppa.cli.commands.apply import _run_apply_pipeline

            _run_apply_pipeline(app_name=app_name, namespace=DEFAULT_NAMESPACE)

            console.print()
            success(f"lstm is live on {app_name}.")
            next_step(f"ppa watch --app {app_name}", "observe live scaling")
            raise typer.Exit()

        # Default: show next step
        next_step(f"ppa apply --app-name {app_name}", "deploy the trained model")

    except ImportError as e:
        error_block(
            "Cannot import training module",
            cause=str(e),
            fix="pip install ppa[model]",
        )
        raise typer.Exit(1) from e
