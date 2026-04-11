"""ppa model — ML model commands (train, evaluate, pipeline, convert)."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from ppa.cli.utils import console, error, heading, success
from ppa.config import (
    ARTIFACTS_DIR,
    CHAMPION_DIR,
    DEFAULT_APP_NAME,
    DEFAULT_CSV,
    DEFAULT_EPOCHS,
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
    DEFAULT_NAMESPACE,
)
from ppa.model.artifacts import artifact_dir, keras_model_path, tflite_model_path

app = typer.Typer(rich_markup_mode="rich")


@app.command("train")
def model_train(
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
    app_name: str = typer.Option(DEFAULT_APP_NAME, "--app-name", "-a", help="App name."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
    target: str = typer.Option(
        DEFAULT_HORIZON, "--target", help="Target column (rps_t3m, rps_t5m, rps_t10m)."
    ),
    epochs: int = typer.Option(DEFAULT_EPOCHS, "--epochs", help="Max training epochs."),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, "--lookback", help="Lookback window steps."),
    patience: int = typer.Option(15, "--patience", help="Early stopping patience."),
    test_split: float = typer.Option(0.1, "--test-split", help="Test set fraction."),
    output_dir: str = typer.Option(
        str(ARTIFACTS_DIR), "--output-dir", help="Output directory for artifacts."
    ),
    target_floor: float = typer.Option(5.0, "--target-floor", help="Minimum target RPS floor."),
) -> None:
    """
    [bold]Train[/] an LSTM model for a prediction horizon.

    Directly invokes the training pipeline with Rich progress.
    """
    heading(f"Train LSTM — {target}")

    if not os.path.exists(csv):
        error(f"CSV not found: {csv}")
        raise typer.Exit(1)

    # Show config
    config = Table(show_header=False, border_style="bright_magenta", padding=(0, 1))
    config.add_column("Key", style="info")
    config.add_column("Value")
    for k, v in {
        "Target": target,
        "CSV": csv,
        "Epochs": str(epochs),
        "Lookback": str(lookback),
        "Patience": str(patience),
    }.items():
        config.add_row(k, v)
    console.print(config)
    console.print()

    try:
        from ppa.model.train import train_model

        with console.status("[info]Training in progress...[/info]", spinner="dots"):
            result = train_model(
                csv_path=csv,
                lookback=lookback,
                epochs=epochs,
                target_col=target,
                app_name=app_name,
                namespace=namespace,
                test_split=test_split,
                output_dir=output_dir,
                target_floor=target_floor,
                early_stopping_patience=patience,
            )

        if result is None:
            error("Training failed")
            raise typer.Exit(1)

        # Results panel
        metrics = result["metrics"]
        paths = result["artifact_paths"]

        results_table = Table(
            title="[bold]Training Results[/]", border_style="green", header_style="bold"
        )
        results_table.add_column("Metric", style="bold")
        results_table.add_column("Value", justify="right")

        results_table.add_row("Val Loss", f"{metrics['val_loss']:.6f}")
        results_table.add_row("Val MAE", f"{metrics['val_mae']:.4f}")
        results_table.add_row("Epochs Run", str(metrics["epochs_run"]))

        console.print()
        console.print(results_table)
        console.print()

        for label, path in paths.items():
            success(f"{label}: {path}")

    except ImportError as e:
        error(f"Cannot import model.train: {e}")
        raise typer.Exit(1) from e


@app.command("evaluate")
def model_evaluate(
    model_path: str = typer.Option(..., "--model", help="Path to .keras model file."),
    scaler_path: str = typer.Option(..., "--scaler", help="Path to feature scaler .pkl."),
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
    target: str = typer.Option(DEFAULT_HORIZON, "--target", help="Target column."),
    output_dir: str = typer.Option(str(ARTIFACTS_DIR), "--output-dir", help="Output directory."),
    meta: str | None = typer.Option(None, "--meta", help="Path to split metadata JSON."),
    target_scaler: str | None = typer.Option(
        None, "--target-scaler", help="Path to target scaler .pkl."
    ),
    test_split: float = typer.Option(0.1, "--test-split", help="Test set fraction."),
    low_traffic: float = typer.Option(
        10.0, "--low-traffic-threshold", help="Min RPS for filtered MAPE."
    ),
) -> None:
    """
    [bold]Evaluate[/] a trained model with detailed metrics and PPA vs HPA comparison.
    """
    heading(f"Evaluate Model — {target}")

    try:
        from ppa.model.evaluate import evaluate_model

        with console.status("[info]Evaluating model...[/info]", spinner="dots"):
            result = evaluate_model(
                model_path=model_path,
                scaler_path=scaler_path,
                csv_path=csv,
                target_col=target,
                output_dir=output_dir,
                meta_path=meta,
                target_scaler_path=target_scaler,
                test_split=test_split,
                low_traffic_threshold=low_traffic,
            )

        if result is None:
            error("Evaluation failed")
            raise typer.Exit(1)

        # Results table
        table = Table(
            title="[bold]Evaluation Results[/]",
            border_style="green",
            header_style="bold",
        )
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")

        table.add_row("MAPE", f"{result['mape']:.2f}%")
        table.add_row("sMAPE", f"{result.get('smape', 0):.2f}%")
        table.add_row("MAE", f"{result['mae']:.4f}")
        table.add_row("RMSE", f"{result['rmse']:.4f}")
        table.add_row("Test samples", str(result["test_samples"]))

        console.print()
        console.print(table)

        # PPA vs HPA comparison
        cmp = Table(
            title="[bold]PPA vs HPA Comparison[/]",
            border_style="bright_cyan",
            header_style="bold",
        )
        cmp.add_column("Metric", style="bold", min_width=24)
        cmp.add_column("PPA", justify="right")
        cmp.add_column("HPA", justify="right")

        cmp.add_row(
            "Avg Replicas",
            f"{result.get('ppa_avg_replicas', 0):.2f}",
            f"{result.get('hpa_avg_replicas', 0):.2f}",
        )
        cmp.add_row(
            "Over-provisioned %",
            f"{result.get('ppa_over_prov_pct', 0):.1f}",
            f"{result.get('hpa_over_prov_pct', 0):.1f}",
        )
        cmp.add_row(
            "Under-provisioned %",
            f"{result.get('ppa_under_prov_pct', 0):.1f}",
            f"{result.get('hpa_under_prov_pct', 0):.1f}",
        )
        cmp.add_row(
            "Replica savings",
            f"[green]{result.get('replica_savings_pct', 0):.1f}%[/green]",
            "—",
        )

        console.print()
        console.print(cmp)

    except ImportError as e:
        error(f"Cannot import model.evaluate: {e}")
        raise typer.Exit(1) from e


@app.command("pipeline")
def model_pipeline(
    app_name: str = typer.Option(
        DEFAULT_APP_NAME, "--app-name", "-a", help="Target application name."
    ),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Target namespace."),
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
    horizons: str = typer.Option(
        "rps_t3m,rps_t5m,rps_t10m", "--horizons", help="Comma-separated targets."
    ),
    epochs: int = typer.Option(DEFAULT_EPOCHS, "--epochs", help="Training epochs."),
    quality_gate: float = typer.Option(25.0, "--quality-gate", help="Gate threshold (%)."),
    gate_metric: str = typer.Option("smape", "--gate-metric", help="Metric for quality gate."),
    patience: int = typer.Option(15, "--patience", help="Early stopping patience."),
    promote: bool = typer.Option(
        False, "--promote-if-better", help="Auto-promote if challenger beats champion."
    ),
) -> None:
    """
    [bold]Full pipeline[/] — train → evaluate → convert for all horizons.

    Runs the complete ML pipeline with quality gates and optional promotion.
    """
    heading("ML Pipeline")

    horizon_list = [h.strip() for h in horizons.split(",")]

    config_table = Table(show_header=False, border_style="bright_magenta", padding=(0, 1))
    config_table.add_column("Key", style="info")
    config_table.add_column("Value")
    config_table.add_row("App", app_name)
    config_table.add_row("Horizons", ", ".join(horizon_list))
    config_table.add_row("Epochs", str(epochs))
    config_table.add_row("Quality gate", f"{gate_metric} ≤ {quality_gate}%")
    config_table.add_row("Auto-promote", str(promote))
    console.print(config_table)
    console.print()

    try:
        from ppa.model.pipeline import run_pipeline

        exit_code = run_pipeline(
            app_name=app_name,
            namespace=namespace,
            csv_path=csv,
            horizons=horizon_list,
            epochs=epochs,
            quality_gate=quality_gate,
            gate_metric=gate_metric,
            patience=patience,
            promote_if_better=promote,
            champion_dir=str(CHAMPION_DIR) if promote else None,
        )

        if exit_code == 0:
            console.print(
                Panel(
                    "[success]Pipeline complete — all horizons passed[/success]",
                    border_style="green",
                )
            )
        else:
            console.print(
                Panel(
                    "[warning]Pipeline complete — some horizons failed quality gate[/warning]",
                    border_style="yellow",
                )
            )

        raise typer.Exit(exit_code)

    except ImportError as e:
        error(f"Cannot import model.pipeline: {e}")
        raise typer.Exit(1) from e


@app.command("convert")
def model_convert(
    app_name: str = typer.Option(DEFAULT_APP_NAME, "--app-name", "-a", help="App name."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
    target: str = typer.Option(DEFAULT_HORIZON, "--target", help="Target column."),
    root_dir: str = typer.Option(str(ARTIFACTS_DIR), "--root-dir", help="Artifact root dir."),
    output: str | None = typer.Option(None, "--output", help="Output .tflite path."),
    no_quantize: bool = typer.Option(False, "--no-quantize", help="Skip quantization."),
) -> None:
    """
    [bold]Convert[/] a Keras model to TFLite format.
    """
    heading("Convert Keras → TFLite")

    try:
        from ppa.model.convert import convert_model

        model_path = keras_model_path(app_name, namespace, target, Path(root_dir))
        output_path = output or str(tflite_model_path(app_name, namespace, target, Path(root_dir)))

        with console.status("[info]Converting model...[/info]", spinner="dots"):
            result = convert_model(
                model_path=str(model_path),
                quantize=not no_quantize,
                output_path=output_path,
            )

        if result:
            success(f"Output: {result['output_path']}")
            success(f"Size: {result['size_kb']:.2f} KB")
        else:
            error("Conversion failed")
            raise typer.Exit(1)

    except ImportError as e:
        error(f"Cannot import model.convert: {e}")
        raise typer.Exit(1) from e


@app.command("push")
def model_push(
    app_name: str = typer.Option("test-app", "--app-name", "-a"),
    namespace: str = typer.Option("default", "--namespace", "-n"),
    horizon: str = typer.Option(
        "rps_t3m,rps_t5m,rps_t10m", "--horizon", "-h", help="Comma-separated horizons"
    ),
    pvc_name: str = typer.Option("ppa-models", "--pvc"),
    image: str = typer.Option("python:3.11-slim", "--image"),
    data: str = typer.Option(None, "--data", "-d", help="Path to training CSV"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """[bold]Push[/] trained models to PVC via loader pod."""
    from ppa.cli.commands.push import push_models

    horizons = [h.strip() for h in horizon.split(",")]
    result = push_models(
        app_name=app_name,
        horizons=horizons,
        namespace=namespace,
        pvc_name=pvc_name,
        image=image,
        data_csv=data,
        dry_run=dry_run,
    )

    if not result.success:
        raise typer.Exit(1)
