"""ppa model — ML model commands (evaluate, convert, push).

Training is now a top-level command: `ppa train`.
Full pipeline is now: `ppa run`.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from ppa.cli.utils import console, error, heading, success
from ppa.config import (
    ARTIFACTS_DIR,
    DEFAULT_APP_NAME,
    DEFAULT_CSV,
    DEFAULT_HORIZON,
    DEFAULT_NAMESPACE,
)
from ppa.model.artifacts import keras_model_path, tflite_model_path

app = typer.Typer(rich_markup_mode="rich")


@app.command("train", hidden=True, deprecated=True)
def model_train() -> None:
    """Deprecated — use `ppa train` instead."""
    from ppa.cli.utils import warn

    warn("ppa model train is deprecated.  Use: ppa train")
    raise typer.Exit()


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


@app.command("pipeline", hidden=True, deprecated=True)
def model_pipeline() -> None:
    """Deprecated — use `ppa run` instead."""
    from ppa.cli.utils import warn

    warn("ppa model pipeline is deprecated.  Use: ppa run")
    raise typer.Exit()


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
