"""ppa debug — Diagnostics for HPA, traffic, and test-app.

Replaces ppa toolbox.
"""

from __future__ import annotations

import subprocess

import typer
from rich.panel import Panel

from ppa.cli.utils import (
    console,
    error_block,
    info,
    run_cmd,
    success,
    warn,
)
from ppa.config import DEFAULT_NAMESPACE, PROJECT_DIR

debug_app = typer.Typer(rich_markup_mode="rich")
traffic_app = typer.Typer(rich_markup_mode="rich")
debug_app.add_typer(traffic_app, name="traffic", help="Manage traffic-gen load generator.")


@debug_app.command("hpa-status")
def debug_hpa_status(
    app_name: str = typer.Option("test-app", "--app", "-a", help="Application name."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
) -> None:
    """Show HPA status for an application."""
    console.print()
    console.print(f"  [bold]HPA Status[/]  ·  {app_name}")
    console.print()

    result = subprocess.run(
        ["kubectl", "get", "hpa", app_name, "-n", namespace],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error_block(
            "HPA not found",
            cause=f"No HPA named {app_name} in namespace {namespace}",
            fix=f"kubectl get hpa -n {namespace}",
        )
        raise typer.Exit(1)

    console.print(Panel(result.stdout.strip(), title="[cyan]Summary[/]", border_style="dim"))


@debug_app.command("test-app")
def debug_test_app() -> None:
    """Manually apply the test-app deployment."""
    console.print()
    info("Deploying test-app...")
    yaml_path = PROJECT_DIR / "data" / "test-app" / "deployment.yaml"
    if not yaml_path.exists():
        error_block(
            "Manifest not found",
            cause=str(yaml_path),
            fix="Ensure the repository is intact",
        )
        raise typer.Exit(1)

    run_cmd(["kubectl", "apply", "-f", str(yaml_path)])
    success("test-app deployment applied")


@traffic_app.command("start")
def traffic_start() -> None:
    """Start the Locust traffic generator."""
    from ppa.cli.commands.startup_steps import step_6_traffic_gen
    console.print()
    info("Starting traffic generator...")
    try:
        step_6_traffic_gen()
        success("Traffic generator started")
    except Exception as e:
        error_block("Failed to start traffic generator", cause=str(e), fix="ppa debug traffic restart")


@traffic_app.command("stop")
def traffic_stop() -> None:
    """Stop the Locust traffic generator."""
    console.print()
    info("Stopping traffic generator...")
    result = subprocess.run(
        ["kubectl", "delete", "deployment", "traffic-gen", "-n", DEFAULT_NAMESPACE],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if "NotFound" in result.stderr:
            warn("Traffic generator is not running")
        else:
            error_block("Failed to stop", cause=result.stderr, fix="kubectl delete deploy/traffic-gen")
            raise typer.Exit(1)
    else:
        success("Traffic generator stopped")
