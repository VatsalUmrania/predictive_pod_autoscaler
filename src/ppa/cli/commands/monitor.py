"""ppa monitor — Live HPA vs PPA dashboard.

Uses Rich Live display for real-time auto-refreshing terminal dashboard.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from typing import cast

import typer
from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ppa.cli.utils import console, query_prometheus, run_cmd_silent

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


def _get_predictions_file() -> str:
    """Get cross-platform predictions file path."""
    return os.path.join(tempfile.gettempdir(), "ppa_predictions.txt")


PREDICTIONS_FILE = _get_predictions_file()
PREDICTION_LOG = "prediction_validation.log"


def _color_accuracy(value: float | None) -> str:
    if value is None:
        return "[italic]N/A[/italic]"
    if value >= 90:
        return f"[bold green]{value:.1f}%[/bold green]"
    if value >= 80:
        return f"[bold blue]{value:.1f}%[/bold blue]"
    return f"[bold red]{value:.1f}%[/bold red]"


def _color_metric(value: str | None, suffix: str = "") -> str:
    if value is None or value == "N/A":
        return "[italic]N/A[/italic]"
    return f"[metric]{value}{suffix}[/metric]"


def _get_ppa_status() -> dict:
    """Get PPA CR status fields."""
    result = run_cmd_silent(
        ["kubectl", "get", "ppa", "test-app-ppa", "-o", "jsonpath={.status}"],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"desired": "?", "current": "?", "predicted_load": "?"}
    try:
        data = json.loads(result.stdout)
        return {
            "desired": str(data.get("desiredReplicas", "?")),
            "current": str(data.get("currentReplicas", "?")),
            "predicted_load": str(data.get("lastPredictedLoad", "?")),
        }
    except (json.JSONDecodeError, KeyError):
        return {"desired": "?", "current": "?", "predicted_load": "?"}


def _get_predicted_rps_from_logs() -> str:
    """Get latest predicted RPS from operator logs."""
    result = run_cmd_silent(
        ["kubectl", "logs", "deployment/ppa-operator", "--tail=30"],
        check=False,
    )
    if result.returncode != 0:
        return "?"
    for line in reversed(result.stdout.splitlines()):
        if "Predicted load:" in line:
            parts = line.split("Predicted load:")
            if len(parts) > 1:
                try:
                    return cast(str, parts[1].strip().split()[0])
                except (IndexError, ValueError):
                    pass
    return "?"


def _build_scaling_panel() -> Panel:
    """Section 1: Current Scaling State."""
    hpa_desired = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "hpa",
                "test-app",
                "-o",
                "jsonpath={.status.desiredReplicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    hpa_current = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "hpa",
                "test-app",
                "-o",
                "jsonpath={.status.currentReplicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    ppa = _get_ppa_status()

    actual = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "deployment",
                "test-app",
                "-o",
                "jsonpath={.status.replicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    ready = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "deployment",
                "test-app",
                "-o",
                "jsonpath={.status.readyReplicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    table = Table(
        show_header=True, border_style="info", header_style="heading", padding=(0, 1)
    )
    table.add_column("Component", style="bold", min_width=12)
    table.add_column("Current", justify="center", min_width=8)
    table.add_column("Desired", justify="center", min_width=8)

    table.add_row("HPA", hpa_current, hpa_desired)
    table.add_row("PPA", ppa["current"], ppa["desired"])
    table.add_row("Deployment", ready, actual)

    return Panel(table, title="[bold]Scaling State[/]", border_style="info")


def _build_metrics_panel() -> Panel:
    """Section 2: Real-time metrics from Prometheus."""
    rps = (
        query_prometheus('sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))')
        or "N/A"
    )
    rps_per = (
        query_prometheus(
            'sum(rate(http_requests_total{pod=~"test-app.*"}[1m]))'
            '/sum(kube_deployment_status_replicas_ready{deployment="test-app",namespace="default"})'
        )
        or "N/A"
    )
    cpu = (
        query_prometheus(
            'sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))'
            '/sum(kube_pod_container_resource_limits{resource="cpu",pod=~"test-app.*"})*100'
        )
        or "N/A"
    )
    p95 = (
        query_prometheus(
            'histogram_quantile(0.95,sum(rate(http_request_duration_seconds_bucket{pod=~"test-app.*"}[1m]))by(le))*1000'
        )
        or "N/A"
    )

    table = Table(show_header=False, border_style="info", padding=(0, 1))
    table.add_column("Metric", style="bold", min_width=16)
    table.add_column("Value", justify="right", min_width=12)

    table.add_row("RPS (total)", _color_metric(rps, " req/s"))
    table.add_row("RPS/replica", _color_metric(rps_per, " req/s"))
    table.add_row("CPU util", _color_metric(cpu, "%"))
    table.add_row("P95 latency", _color_metric(p95, " ms"))

    return Panel(table, title="[bold]Real-Time Metrics[/]", border_style="info")


def _build_prediction_panel() -> Panel:
    """Section 3: PPA prediction info."""
    ppa = _get_ppa_status()
    pred_rps = _get_predicted_rps_from_logs()

    hpa_cpu = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "hpa",
                "test-app",
                "-o",
                "jsonpath={.status.currentMetrics[0].resource.current.averageUtilization}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    table = Table(show_header=False, border_style="step", padding=(0, 1))
    table.add_column("Metric", style="bold", min_width=22)
    table.add_column("Value", justify="right", min_width=12)

    table.add_row("PPA predicted load", _color_metric(ppa["predicted_load"], " req/s"))
    table.add_row("PPA latest (logs)", _color_metric(pred_rps, " req/s"))
    table.add_row("PPA desired replicas", _color_metric(ppa["desired"]))
    table.add_row("HPA CPU (trigger=50%)", _color_metric(hpa_cpu, "%"))

    return Panel(table, title="[bold]PPA Prediction (t+10m)[/]", border_style="step")


def _build_comparison_panel() -> Panel:
    """Section 5: Who's scaling better."""
    hpa = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "hpa",
                "test-app",
                "-o",
                "jsonpath={.status.desiredReplicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    ppa = _get_ppa_status()["desired"]

    actual = (
        run_cmd_silent(
            [
                "kubectl",
                "get",
                "deployment",
                "test-app",
                "-o",
                "jsonpath={.status.replicas}",
            ],
            check=False,
        ).stdout.strip()
        or "?"
    )

    try:
        hpa_int = int(hpa)
        ppa_int = int(ppa)
        if hpa_int > ppa_int:
            verdict = "[bold magenta][ HPA MORE CONSERVATIVE ][/bold magenta] (wants more replicas)"
        elif ppa_int > hpa_int:
            verdict = "[bold green][ PPA MORE CONSERVATIVE ][/bold green] (forecasts higher load)"
        else:
            verdict = "[bold blue][ MATCH ][/bold blue] (both want same replicas)"
    except ValueError:
        verdict = "[italic]Waiting for data...[/italic]"

    content = f"{verdict}\n\n  Actual running: [bold]{actual}[/bold] replicas"
    return Panel(content, title="[bold]Scaling Comparison[/]", border_style="success")


def _build_accuracy_panel() -> Panel:
    """Section 6: Overall prediction accuracy stats."""
    if not os.path.exists(PREDICTION_LOG):
        return Panel(
            "[dim]No validation data yet[/dim]",
            title="[bold]Accuracy[/]",
            border_style="bright_yellow",
        )

    try:
        with open(PREDICTION_LOG) as f:
            lines = f.readlines()[1:]  # skip header

        if not lines:
            return Panel(
                "[italic]Waiting for 10+ minutes...[/italic]",
                title="[bold]Accuracy[/]",
                border_style="warning",
            )

        total = len(lines)
        accuracies = []
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) >= 5:
                try:
                    accuracies.append(float(parts[4]))
                except ValueError:
                    pass

        avg_acc = sum(accuracies) / len(accuracies) if accuracies else 0

        content = (
            f"  Total validations: [bold]{total}[/bold]\n"
            f"  Avg accuracy:      {_color_accuracy(avg_acc)}"
        )
    except Exception:
        content = "[italic]Error reading log[/italic]"

    return Panel(content, title="[bold]Prediction Accuracy[/]", border_style="warning")


def _build_dashboard() -> Layout:
    """Build the full dashboard layout."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = Panel(
        Align.center(
            Text(
                f"HPA vs PPA — Live t+10 Prediction Validation\n{now}", style="heading"
            )
        ),
        border_style="info",
    )

    # Build panels
    scaling = _build_scaling_panel()
    metrics = _build_metrics_panel()
    prediction = _build_prediction_panel()
    comparison = _build_comparison_panel()
    accuracy = _build_accuracy_panel()

    # Layout
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


@app.callback(invoke_without_command=True)
def monitor(
    ctx: typer.Context,
    interval: int = typer.Option(
        15, "--interval", "-i", help="Refresh interval in seconds."
    ),
) -> None:
    """
    [bold]Live monitoring dashboard[/] — HPA vs PPA comparison with prediction validation.

    Auto-refreshes every [bold]--interval[/] seconds. Press [bold]Ctrl+C[/] to exit.
    """
    if ctx.invoked_subcommand is not None:
        return

    # Ensure predictions file exists
    if not os.path.exists(PREDICTIONS_FILE):
        with open(PREDICTIONS_FILE, "w"):
            pass

    if not os.path.exists(PREDICTION_LOG):
        with open(PREDICTION_LOG, "w") as f:
            f.write(
                "timestamp,predicted_rps,actual_rps_10min_later,error_percent,accuracy\n"
            )

    console.print()
    console.print("[italic]Starting live dashboard — Ctrl+C to exit[/italic]")
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
        console.print("[success]✓[/success] Monitor stopped")
