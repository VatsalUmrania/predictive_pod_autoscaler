"""ppa toolbox — Collection of utility and diagnostic commands."""

from __future__ import annotations

import subprocess

import typer
from rich.panel import Panel

from ppa.config import DEFAULT_NAMESPACE, PROJECT_DIR
from ppa.cli.utils import (
    console,
    error,
    heading,
    info,
    run_cmd,
    run_cmd_silent,
    success,
    warn,
)

app = typer.Typer(rich_markup_mode="rich")
traffic_app = typer.Typer(rich_markup_mode="rich")


@app.command("deploy-test-app")
def toolbox_deploy_test_app() -> None:
    """
    [bold]Deploy test-app[/] — applies the deployment YAML.
    """
    heading("Deploy Test Application")
    yaml_path = PROJECT_DIR / "data" / "test-app" / "deployment.yaml"

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

    console.print(
        Panel(result.stdout.strip(), title="[bold]Summary[/]", border_style="bright_cyan")
    )

    # 2. Get describe
    info("Fetching detailed description...")
    describe = run_cmd_silent(["kubectl", "describe", "hpa", app_name, "-n", namespace])
    console.print(
        Panel(
            describe.stdout.strip(),
            title="[bold]Detailed Description[/]",
            border_style="bright_magenta",
        )
    )


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


# ============================================================================
# Traffic Generation Commands
# ============================================================================


@traffic_app.command("start")
def traffic_start(
    low_users: int = typer.Option(50, "--low-users", help="Low phase user count."),
    med_users: int = typer.Option(250, "--med-users", help="Medium phase user count."),
    spike_users: int = typer.Option(1000, "--spike-users", help="Spike phase user count."),
    fast_mode: bool = typer.Option(False, "--fast-mode", help="Enable fast mode (1s = 1min)."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
) -> None:
    """[bold]Start[/] traffic generation against test-app."""
    heading("Starting Traffic Generation")

    locustfile_path = PROJECT_DIR / "tests" / "locustfile.py"
    if not locustfile_path.exists():
        error(f"Locustfile not found: {locustfile_path}")
        raise typer.Exit(1)

    # Create ConfigMap from locustfile
    info("Creating ConfigMap from locustfile...")
    result = run_cmd_silent(
        [
            "kubectl",
            "create",
            "configmap",
            "traffic-gen-locustfile",
            f"--from-file=locustfile.py={locustfile_path}",
            "-n",
            namespace,
            "--dry-run=client",
            "-o",
            "yaml",
        ]
    )

    if result.returncode != 0:
        error("Failed to create ConfigMap YAML")
        raise typer.Exit(1)

    # Apply ConfigMap (may already exist, that's ok)
    result = subprocess.run(
        [
            "kubectl",
            "apply",
            "-f",
            "-",
            "-n",
            namespace,
        ],
        input=result.stdout,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error("Failed to apply ConfigMap")
        console.print(result.stderr)
        raise typer.Exit(1)

    success("ConfigMap ready")

    # Build deployment YAML with env var overrides
    deployment_path = PROJECT_DIR / "deploy" / "traffic-gen-deployment.yaml"
    if not deployment_path.exists():
        error(f"Deployment YAML not found: {deployment_path}")
        raise typer.Exit(1)

    # Read deployment and inject env vars
    with open(deployment_path) as f:
        deployment_yaml = f.read()

    # Replace env values
    env_replacements = {
        'value: "50"': f'value: "{low_users}"',  # STAGE_LOW_USERS
        'value: "250"': f'value: "{med_users}"',  # STAGE_MED_USERS (first occurrence)
        'value: "1000"': f'value: "{spike_users}"',  # STAGE_SPIKE_USERS
        'value: "false"': f'value: "{str(fast_mode).lower()}"',  # FAST_MODE
    }

    for old, new in env_replacements.items():
        deployment_yaml = deployment_yaml.replace(old, new, 1)

    info(
        f"Applying deployment with: low={low_users}, med={med_users}, spike={spike_users}, fast_mode={fast_mode}"
    )

    # Apply deployment
    result = subprocess.run(
        ["kubectl", "apply", "-f", "-", "-n", namespace],
        input=deployment_yaml,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        error("Failed to apply deployment")
        console.print(result.stderr)
        raise typer.Exit(1)

    success("Traffic generation started")
    info("Waiting for pod to be ready...")

    # Wait for rollout
    wait_result = subprocess.run(
        ["kubectl", "rollout", "status", "deployment/traffic-gen", "-n", namespace],
        capture_output=True,
        text=True,
    )

    if wait_result.returncode == 0:
        success("Traffic generation pod is running")
    else:
        warn("Pod may not be ready yet, check with 'ppa toolbox traffic status'")


@traffic_app.command("stop")
def traffic_stop(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
) -> None:
    """[bold]Stop[/] traffic generation."""
    heading("Stopping Traffic Generation")

    info("Deleting traffic-gen deployment...")
    result = run_cmd_silent(
        ["kubectl", "delete", "deployment", "traffic-gen", "-n", namespace],
        check=False,
    )

    if result.returncode == 0:
        success("Traffic generation stopped")
    else:
        if "not found" in result.stderr.lower():
            warn("Traffic generation not running")
        else:
            error("Failed to stop traffic generation")
            console.print(result.stderr)
            raise typer.Exit(1)


@traffic_app.command("restart")
def traffic_restart(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
) -> None:
    """[bold]Restart[/] traffic generation."""
    heading("Restarting Traffic Generation")

    info("Restarting deployment...")
    result = run_cmd_silent(
        ["kubectl", "rollout", "restart", "deployment/traffic-gen", "-n", namespace],
        check=False,
    )

    if result.returncode != 0:
        error("Failed to restart deployment")
        console.print(result.stderr)
        raise typer.Exit(1)

    success("Deployment restart triggered")
    info("Waiting for rollout...")

    wait_result = subprocess.run(
        ["kubectl", "rollout", "status", "deployment/traffic-gen", "-n", namespace],
        capture_output=True,
        text=True,
    )

    if wait_result.returncode == 0:
        success("Traffic generation restarted")
    else:
        warn("Rollout may still be in progress, check with 'ppa toolbox traffic status'")


@traffic_app.command("status")
def traffic_status(
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Namespace."),
    tail: int = typer.Option(20, "--tail", "-t", help="Log lines to show."),
) -> None:
    """[bold]Status[/] of traffic generation."""
    heading("Traffic Generation Status")

    # Check deployment
    result = run_cmd_silent(
        ["kubectl", "get", "deployment", "traffic-gen", "-n", namespace],
        check=False,
    )

    if result.returncode != 0:
        error("Traffic generation not running")
        raise typer.Exit(1)

    console.print(
        Panel(
            result.stdout.strip(),
            title="[bold]Deployment[/]",
            border_style="bright_cyan",
        )
    )

    # Get pod status
    result = run_cmd_silent(
        ["kubectl", "get", "pods", "-l", "app=traffic-gen", "-n", namespace],
        check=False,
    )

    if result.returncode == 0:
        console.print(
            Panel(
                result.stdout.strip(),
                title="[bold]Pod Status[/]",
                border_style="bright_magenta",
            )
        )

    # Show recent logs
    info(f"Recent logs (last {tail} lines):")
    result = run_cmd_silent(
        [
            "kubectl",
            "logs",
            "deployment/traffic-gen",
            "-n",
            namespace,
            f"--tail={tail}",
        ],
        check=False,
    )

    if result.returncode == 0:
        console.print(result.stdout)
    else:
        info("(No logs available yet)")


# Add traffic app to main app
app.add_typer(traffic_app, name="traffic", help="Traffic generation commands.")
