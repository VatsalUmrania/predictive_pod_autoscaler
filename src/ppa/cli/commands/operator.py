"""ppa operator — Kubernetes operator lifecycle management.

Commands to build, deploy, restart, and monitor the PPA operator.
"""

from __future__ import annotations

import os
import subprocess

import typer
from rich.table import Table

from ppa.cli.utils import (
    console,
    error,
    get_minikube_docker_env,
    heading,
    info,
    kubectl,
    run_cmd,
    success,
    warn,
)
from ppa.config import DEPLOY_DIR, PROJECT_DIR

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


def _get_docker_env() -> dict:
    """Get Docker environment variables for Minikube."""
    env = {**os.environ, **get_minikube_docker_env()}
    env["DOCKER_BUILDKIT"] = "0"
    return env


def _check_minikube() -> bool:
    """Check if Minikube is running."""
    result = subprocess.run(
        ["minikube", "status", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        warn("Minikube is not running. Start it with: minikube start")
        return False
    import json

    try:
        status = json.loads(result.stdout)
        if status.get("Host") != "Running" or status.get("Kubeconfig") != "Configured":
            warn("Minikube is not ready. Run: minikube start")
            return False
    except (json.JSONDecodeError, KeyError):
        pass
    return True


@app.callback(invoke_without_command=True)
def operator(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace."),
) -> None:
    """[bold]Operator lifecycle management[/] — build, deploy, restart, and monitor."""
    ctx.ensure_object(dict)
    ctx.obj["namespace"] = namespace


@app.command("build")
def build_cmd(
    ctx: typer.Context,
    image: str = typer.Option("ppa-operator:latest", "--image", "-i", help="Docker image tag."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Build without Docker cache."),
) -> None:
    """[bold]Build[/] the PPA operator Docker image."""
    heading("Building PPA Operator Image")

    if not _check_minikube():
        raise typer.Exit(1)

    info("Using Minikube Docker daemon")
    info(f"Image: {image}")

    dockerfile = PROJECT_DIR / "src" / "ppa" / "operator" / "Dockerfile"
    if not dockerfile.exists():
        error(f"Dockerfile not found at {dockerfile}")
        raise typer.Exit(1)

    info(f"Dockerfile: {dockerfile}")

    cmd = ["docker", "build", "-t", image]
    if no_cache:
        cmd.append("--no-cache")
    cmd.extend(["-f", str(dockerfile), str(PROJECT_DIR)])

    env = _get_docker_env()
    result = subprocess.run(cmd, env=env)

    if result.returncode != 0:
        error("Docker build failed")
        raise typer.Exit(1)

    success(f"Image built: {image}")


@app.command("deploy")
def deploy_cmd(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace."),
    image: str = typer.Option(
        "ppa-operator:latest", "--image", "-i", help="Docker image to deploy."
    ),
) -> None:
    """[bold]Deploy[/] operator manifests to Kubernetes."""
    heading("Deploying PPA Operator")

    info(f"Namespace: {namespace}")
    info(f"Image: {image}")

    kubectl("apply", "-f", str(DEPLOY_DIR / "operator-deployment.yaml"), namespace=namespace)
    success("Applied: operator-deployment.yaml")

    servicemonitor_path = DEPLOY_DIR / "operator-servicemonitor.yaml"
    if servicemonitor_path.exists():
        kubectl("apply", "-f", str(servicemonitor_path))
        success("Applied: operator-servicemonitor.yaml")
    else:
        warn("Manifest not found: operator-servicemonitor.yaml")

    info("Waiting for rollout...")
    run_cmd(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/ppa-operator",
            f"--namespace={namespace}",
            "--timeout=120s",
        ],
        title="Operator rollout",
    )

    success("Operator deployed")


@app.command("restart")
def restart_cmd(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace."),
    image: str = typer.Option(
        "ppa-operator:latest", "--image", "-i", help="Docker image to deploy."
    ),
) -> None:
    """[bold]Restart[/] the PPA operator (build + deploy + rollout)."""
    namespace = ctx.obj.get("namespace", namespace)

    heading("Restarting PPA Operator")

    if not _check_minikube():
        raise typer.Exit(1)

    info(f"Image: {image}")

    dockerfile = PROJECT_DIR / "src" / "ppa" / "operator" / "Dockerfile"
    if not dockerfile.exists():
        error(f"Dockerfile not found at {dockerfile}")
        raise typer.Exit(1)

    info("Building image...")
    cmd = ["docker", "build", "-t", image, "-f", str(dockerfile), str(PROJECT_DIR)]
    env = _get_docker_env()
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        error("Docker build failed")
        raise typer.Exit(1)
    success("Image built")

    info("Rolling restart...")
    kubectl("rollout", "restart", "deployment/ppa-operator", namespace=namespace)
    run_cmd(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/ppa-operator",
            f"--namespace={namespace}",
            "--timeout=120s",
        ],
        title="Operator rollout",
    )
    success("Operator restarted")


@app.command("status")
def status_cmd(
    ctx: typer.Context,
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace."),
) -> None:
    """[bold]Status[/] of the PPA operator deployment."""
    heading("PPA Operator Status")

    result = kubectl("get", "deployment", "ppa-operator", namespace=namespace, check=False)

    if result.returncode != 0:
        error("Operator deployment not found")
        raise typer.Exit(1)

    table = Table(show_header=True, border_style="cyan")
    table.add_column("NAME", style="cyan")
    table.add_column("READY", style="green")
    table.add_column("UP-TO-DATE", style="yellow")
    table.add_column("AVAILABLE", style="magenta")
    table.add_column("AGE", style="dim")

    lines = result.stdout.strip().split("\n")
    if len(lines) > 1:
        parts = lines[1].split()
        if len(parts) >= 5:
            table.add_row(*parts[:5])

    console.print(table)

    pod_result = kubectl("get", "pods", "-l", "app=ppa-operator", namespace=namespace, check=False)
    if pod_result.returncode == 0:
        console.print()
        console.print(pod_result.stdout)

    events_result = kubectl(
        "get",
        "events",
        "--field-selector",
        "involvedObject.name=ppa-operator",
        "--sort-by",
        ".lastTimestamp",
        namespace=namespace,
        check=False,
    )
    if events_result.returncode == 0 and events_result.stdout.strip():
        lines = events_result.stdout.strip().split("\n")
        if len(lines) > 1:
            console.print()
            info("Recent events:")
            console.print("\n".join(lines[-5:]))


if __name__ == "__main__":
    app()
