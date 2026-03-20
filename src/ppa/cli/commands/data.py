"""ppa data — Data collection commands (export, validate, health)."""

from __future__ import annotations

import os
import sys

import typer
from rich.table import Table

from ppa.cli.utils import console, error, heading, info, success, warn
from ppa.config import DEFAULT_APP_NAME, DEFAULT_CSV, PROJECT_DIR, TRAINING_DATA_DIR

app = typer.Typer(rich_markup_mode="rich")


@app.command("export")
def data_export(
    app_name: str = typer.Option(
        DEFAULT_APP_NAME, "--app-name", "-a", help="Target application name."
    ),
    hours: int = typer.Option(168, "--hours", help="Hours of data to collect."),
    step: str = typer.Option("1m", "--step", help="Prometheus query step (e.g. 15s, 1m)."),
    resample: str | None = typer.Option(None, "--resample", help="Resample interval (e.g. 1m)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run without saving the CSV."),
) -> None:
    """
    [bold]Export training data[/] from Prometheus into a CSV dataset.

    Collects the 14-dimensional feature set and builds prediction targets.
    """
    heading("Data Export")

    config_table = Table(show_header=False, border_style="bright_cyan", padding=(0, 1))
    config_table.add_column("Key", style="info")
    config_table.add_column("Value")
    config_table.add_row("App", app_name)
    config_table.add_row("Hours", str(hours))
    config_table.add_row("Step", step)
    config_table.add_row("Resample", resample or "None")
    config_table.add_row("Dry run", str(dry_run))
    console.print(config_table)

    try:
        from ppa.dataflow.export_training_data import (
            add_segment_ids,
            build_dataset_health,
            build_feature_dataframe,
            write_health_report,
        )

        with console.status("[info]Collecting metrics from Prometheus...[/info]", spinner="dots"):
            df, quality_stats = build_feature_dataframe(
                app_name=app_name,
                hours=hours,
                step=step,
                resample=resample,
            )

        df["segment_id"] = 0
        df = add_segment_ids(df)

        info(f"Collected {len(df)} rows with {len(df.columns)} features")

        if not dry_run:
            output_path = str(TRAINING_DATA_DIR / f"{app_name}.csv")
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            df.to_csv(output_path)
            success(f"Saved dataset → {output_path}")

            health = build_dataset_health(df)
            health_path = write_health_report(output_path, health)
            success(f"Health report → {health_path}")
        else:
            info("DRY RUN — dataset not saved")

        # Display health
        health = build_dataset_health(df)
        _show_health_table(health)

    except Exception as e:
        error(f"Export failed: {e}")
        raise typer.Exit(1) from e


@app.command("validate")
def data_validate(
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV to validate."),
) -> None:
    """
    [bold]Validate[/] an existing training dataset for schema and quality.
    """
    heading("Data Validation")

    if not os.path.exists(csv):
        error(f"CSV not found: {csv}")
        raise typer.Exit(1)

    sys.path.insert(0, str(PROJECT_DIR))

    try:
        import pandas as pd

        from ppa.common.feature_spec import FEATURE_COLUMNS, TARGET_COLUMNS

        df = pd.read_csv(csv, index_col="timestamp", parse_dates=True)
        info(f"Loaded {len(df)} rows from {csv}")

        # Schema check
        missing_features = [c for c in FEATURE_COLUMNS if c not in df.columns]
        missing_targets = [c for c in TARGET_COLUMNS if c not in df.columns]

        if not missing_features and not missing_targets:
            success("Schema matches: all 14 features + 3 targets present")
        else:
            if missing_features:
                warn(f"Missing features: {missing_features}")
            if missing_targets:
                warn(f"Missing targets: {missing_targets}")

        # NaN check
        nan_count = df[FEATURE_COLUMNS].isna().sum().sum() if not missing_features else -1
        if nan_count == 0:
            success("Zero NaN values in feature columns")
        elif nan_count > 0:
            warn(f"Found {nan_count} NaN values in feature columns")

        # Row count
        if len(df) >= 1000:
            success(f"Dataset size adequate: {len(df)} rows")
        else:
            warn(f"Dataset may be small: {len(df)} rows (recommend ≥1000)")

    except Exception as e:
        error(f"Validation failed: {e}")
        raise typer.Exit(1) from e


@app.command("health")
def data_health(
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
) -> None:
    """
    [bold]Show dataset health report[/] as a styled table.
    """
    heading("Dataset Health")

    if not os.path.exists(csv):
        error(f"CSV not found: {csv}")
        raise typer.Exit(1)

    # Check for .health.json sidecar
    health_json = csv.replace(".csv", ".health.json")
    if os.path.exists(health_json):
        import json

        with open(health_json) as f:
            health = json.load(f)
        _show_health_table(health)
    else:
        try:
            import pandas as pd

            from ppa.dataflow.export_training_data import build_dataset_health

            df = pd.read_csv(csv, index_col="timestamp", parse_dates=True)
            health = build_dataset_health(df)
            _show_health_table(health)
        except Exception as e:
            error(f"Cannot build health report: {e}")
            raise typer.Exit(1) from e


def _show_health_table(health: dict) -> None:
    table = Table(
        title="[bold bright_cyan]Dataset Health Report[/]",
        border_style="bright_cyan",
        header_style="bold",
    )
    table.add_column("Metric", style="info", min_width=20)
    table.add_column("Value", justify="right")

    table.add_row("Total rows", str(health.get("rows", "?")))
    table.add_row("Total features", str(health.get("features", "?")))
    table.add_row("Segments", str(health.get("segment_count", "?")))
    table.add_row("Weekend rows", str(health.get("weekend_rows", "?")))
    table.add_row("Weekday rows", str(health.get("weekday_rows", "?")))

    if health.get("max_gap"):
        table.add_row("Max time gap", str(health["max_gap"]))

    date_range = health.get("date_range", {})
    if date_range.get("start"):
        table.add_row("Date range", f"{date_range['start']} → {date_range['end']}")

    console.print()
    console.print(table)
