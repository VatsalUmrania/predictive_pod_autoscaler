"""ppa watch — Live dashboard: HPA vs PPA with predictions.

Replaces `ppa monitor`. Reuses all dashboard panel builders from
the original monitor.py implementation.
"""

from __future__ import annotations

import time
from datetime import datetime

import typer
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ppa.cli.commands.monitor import (
    _build_accuracy_panel,
    _build_comparison_panel,
    _build_metrics_panel,
    _build_prediction_panel,
    _build_scaling_panel,
)
from ppa.cli.utils import console


def _build_dashboard() -> Layout:
    """Build the full dashboard layout."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = Panel(
        Align.center(
            Text(
                f"HPA vs PPA — Live t+10 Prediction Validation\n{now}",
                style="bold cyan",
            )
        ),
        border_style="dim",
    )

    scaling = _build_scaling_panel()
    metrics = _build_metrics_panel()
    prediction = _build_prediction_panel()
    comparison = _build_comparison_panel()
    accuracy = _build_accuracy_panel()

    layout = Layout()
    layout.split_column(
        Layout(header, name="header", size=5),
        Layout(name="top", size=10),
        Layout(name="middle", size=10),
        Layout(name="bottom", size=7),
    )
    layout["top"].split_row(
        Layout(scaling, name="scaling"),
        Layout(metrics, name="metrics"),
    )
    layout["middle"].split_row(
        Layout(prediction, name="prediction"),
        Layout(comparison, name="comparison"),
    )
    layout["bottom"].split_row(
        Layout(accuracy, name="accuracy"),
    )

    return layout


def watch_cmd(
    interval: int = typer.Option(
        15, "--interval", "-i", help="Refresh interval in seconds."
    ),
    app: str | None = typer.Option(
        None, "--app", "-a", help="Filter dashboard to a specific app."
    ),
) -> None:
    """Live dashboard — HPA vs PPA with prediction validation.

    Auto-refreshes every --interval seconds. Press Ctrl+C to exit.

    \b
    EXAMPLES
      ppa watch                  # default 15s refresh
      ppa watch -i 5             # faster 5s refresh
      ppa watch --app myapp      # filter to specific app

    \b
    REQUIRES
      • ppa init completed  (infrastructure running)
      • ppa apply completed (operator deployed)
    """
    console.print()
    console.print("  Starting live dashboard...  [dim]Ctrl+C to exit[/]")
    console.print()

    try:
        with Live(
            _build_dashboard(),
            console=console,
            refresh_per_second=0.5,
            screen=True,
        ) as live:
            while True:
                live.update(_build_dashboard())
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print()
        console.print("  [bold green]✓[/]  Dashboard stopped.")
