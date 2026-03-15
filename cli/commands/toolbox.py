"""ppa toolbox — Collection of utility and diagnostic commands."""

from __future__ import annotations

import os
import subprocess

import typer
from rich.panel import Panel

from cli.config import DEFAULT_NAMESPACE, PROJECT_DIR
from cli.utils import console, error, heading, info, run_cmd, run_cmd_silent, success

app = typer.Typer(rich_markup_mode="rich")


@app.command("deploy-test-app")
def toolbox_deploy_test_app() -> None:
    """
    [bold]Deploy test-app[/] — applies the deployment YAML.
    """
    heading("Deploy Test Application")
    yaml_path = PROJECT_DIR / "data-collection" / "test-app-deployment.yaml"
    
    if not yaml_path.exists():
        error(f"Deployment YAML not found: {yaml_path}")
        raise typer.Exit(1)
    
    run_cmd(["kubectl", "apply", "-f", str(yaml_path)], title="Applying test-app deployment")
    success("test-app deployment applied")


@app.command("hpa-status")
def toolbox_hpa_status(
    app_name: str = typer.Option("test-app", "--app", "-a", help="Application name."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
) -> None:
    """
    [bold]HPA status[/] — shows current replicas and detailed description.
    """
    heading(f"HPA Status: {app_name}")
    
    # 1. Get summary
    result = run_cmd_silent(["kubectl", "get", "hpa", app_name, "-n", namespace], check=False)
    if result.returncode != 0:
        error(f"HPA '{app_name}' not found in namespace '{namespace}'")
        return
    
    console.print(Panel(result.stdout.strip(), title="[bold]Summary[/]", border_style="bright_cyan"))
    
    # 2. Get describe
    info("Fetching detailed description...")
    describe = run_cmd_silent(["kubectl", "describe", "hpa", app_name, "-n", namespace])
    console.print(Panel(describe.stdout.strip(), title="[bold]Detailed Description[/]", border_style="bright_magenta"))


@app.command("hpa-watch")
def toolbox_hpa_watch(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
    interval: int = typer.Option(2, "--interval", "-i", help="Watch interval in seconds."),
) -> None:
    """
    [bold]Watch HPAs[/] — live view of scaling status.
    """
    heading(f"Watching HPAs in {namespace}")
    info("Press [bold]Ctrl+C[/bold] to stop.")
    
    try:
        # Use subprocess.run directly for interactive watch
        subprocess.run(["watch", "-n", str(interval), "kubectl", "get", "hpa", "-n", namespace])
    except KeyboardInterrupt:
        console.print()
        success("Stopped watching")


@app.command("logs")
def toolbox_logs(
    component: str = typer.Argument(..., help="Component name (traffic, operator, test-app)."),
    follow: bool = typer.Option(True, "--follow", "-f", help="Follow log stream."),
    tail: int = typer.Option(50, "--tail", "-t", help="Number of lines to show."),
) -> None:
    """
    [bold]View component logs[/] — streams logs from deployments.
    """
    mapping = {
        "traffic": "deployment/traffic-gen",
        "operator": "deployment/ppa-operator",
        "test-app": "deployment/test-app",
    }
    
    target = mapping.get(component.lower())
    if not target:
        error(f"Unknown component: {component}. Valid: {', '.join(mapping.keys())}")
        raise typer.Exit(1)
    
    heading(f"Logs: {component}")
    
    cmd = ["kubectl", "logs"]
    if follow:
        cmd += ["-f"]
    cmd += [f"--tail={tail}", target]
    
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        console.print()
        success("Stopped log stream")
