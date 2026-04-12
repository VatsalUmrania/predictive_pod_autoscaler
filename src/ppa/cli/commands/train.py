"""ppa train — Train the LSTM model.

Top-level command (previously ppa model train). Delegates to
ppa.model.train.train_model() for business logic.
"""

from __future__ import annotations

import os
import time

import typer

from ppa.cli.utils import (
    console,
    error_block,
    info,
    next_step,
    success,
    warn,
)
from ppa.config import (
    DEFAULT_APP_NAME,
    DEFAULT_EPOCHS,
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
    DEFAULT_NAMESPACE,
    TRAINING_DATA_DIR,
)


def train_cmd(
    app: str | None = typer.Option(
        None, "--app", "-a", help="App name (required if multiple apps registered)."
    ),
    horizon: str = typer.Option(
        DEFAULT_HORIZON, "--horizon", help="Prediction horizon target column."
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
) -> None:
    """Train an LSTM model on Prometheus metrics.

    \b
    EXAMPLES
      ppa train                             # train with all defaults
      ppa train --epochs 100 --eval         # train and inspect quality
      ppa train --apply                     # train then deploy
      ppa train --csv ./data/metrics.csv    # use local data file
      ppa train --horizon rps_t5m           # 5-minute prediction horizon

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

    # Start training header
    console.print()
    console.print(f"  Training  [bold]lstm[/]  ·  {app_name}  ·  {epochs} epochs")
    console.print()

    try:
        from ppa.model.train import train_model

        start_time = time.time()

        with console.status("[cyan]Training in progress...[/]", spinner="dots"):
            result = train_model(
                csv_path=csv_path,
                lookback=lookback,
                epochs=epochs,
                target_col=horizon,
                app_name=app_name,
                namespace=DEFAULT_NAMESPACE,
                test_split=0.1,
                output_dir=None,
                target_floor=5.0,
                early_stopping_patience=patience,
            )

        duration = time.time() - start_time
        duration_str = f"{int(duration // 60)}m {int(duration % 60)}s"

        if result is None:
            error_block(
                "Training failed",
                cause="train_model() returned None",
                fix=f"ppa data validate --app {app_name}",
            )
            raise typer.Exit(1)

        metrics = result.get("metrics", {})
        paths = result.get("artifact_paths", {})
        metrics.get("val_loss", 0)
        val_mae = metrics.get("val_mae", 0)
        epochs_run = metrics.get("epochs_run", epochs)

        # Calculate MAPE (approximate from MAE if not available)
        mape = metrics.get("mape", val_mae * 100 if val_mae < 1 else val_mae)

        # Check accuracy threshold
        accuracy_warning = mape > 15.0

        if accuracy_warning:
            warn("Training complete with degraded accuracy")
        else:
            success("Training complete")

        console.print()
        console.print("     Version      lstm")
        console.print(f"     Accuracy     {mape:.1f}% MAPE")
        console.print(f"     Duration     {duration_str}")
        console.print(f"     Epochs       {epochs_run}/{epochs}")

        if paths:
            model_path = paths.get("keras_model", paths.get("model", ""))
            if model_path:
                console.print(f"     Saved        {model_path}")

        if accuracy_warning:
            console.print()
            console.print("     Low accuracy may indicate insufficient or noisy training data.")
            console.print(f"     Inspect data:  [bold]ppa data validate --app {app_name}[/]")

        # Handle --eval (show metrics, skip deploy)
        if eval_only:
            console.print()
            info("Evaluation mode — deployment skipped.")
            if not accuracy_warning:
                next_step(
                    f"ppa apply --app-name {app_name}",
                    "deploy the trained model",
                )
            raise typer.Exit()

        # Handle --apply (chain into apply)
        if apply_after:
            console.print()
            console.print(f"  Deploying  lstm  →  {app_name}")
            console.print()

            from ppa.cli.commands.apply import _run_apply_pipeline

            _run_apply_pipeline(app_name=app_name, namespace=DEFAULT_NAMESPACE)

            console.print()
            success(f"Done.  lstm is live on {app_name}.")
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
