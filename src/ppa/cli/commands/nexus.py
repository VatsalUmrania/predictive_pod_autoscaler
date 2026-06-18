"""ppa nexus — NEXUS (NATS + nexus-api) lifecycle management.

Commands to apply, verify, and monitor the in-cluster NEXUS services.
Run inside Minikube — all connections are Kubernetes service DNS.
"""

from __future__ import annotations

import subprocess

import typer
from rich.console import Console
from rich.table import Table

from ppa.cli.utils import error, run_cmd, success, warn
from ppa.config import PROJECT_DIR

console = Console()
app = typer.Typer(
    rich_markup_mode="rich",
    invoke_without_command=True,
    help="[dim]NEXUS (NATS + nexus-api) in Kubernetes[/dim]",
    no_args_is_help=True,
)


@app.callback(invoke_without_command=True)
def nexus_main() -> None:
    """NEXUS services live inside the Minikube cluster (namespace: nexus).

    No Docker Compose, no host.minikube.internal hack.
    All PPA ↔ NEXUS connections use Kubernetes service DNS.
    """
    pass


@app.command("apply")
def apply_cmd(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show kubectl apply output"),
) -> None:
    """Apply NEXUS manifests (nats-statefulset + nexus-api-deployment)."""
    nats_manifest = PROJECT_DIR / "deploy" / "nexus" / "nats-statefulset.yaml"
    nexus_manifest = PROJECT_DIR / "deploy" / "nexus" / "nexus-api-deployment.yaml"

    if not nats_manifest.exists():
        error(f"NATS manifest not found: {nats_manifest}")
        raise typer.Exit(1)
    if not nexus_manifest.exists():
        error(f"nexus-api manifest not found: {nexus_manifest}")
        raise typer.Exit(1)

    run_cmd(
        ["kubectl", "apply", "-f", str(nats_manifest)],
        title="Apply NATS StatefulSet (namespace: nexus)",
    )
    run_cmd(
        ["kubectl", "apply", "-f", str(nexus_manifest)],
        title="Apply nexus-api Deployment (namespace: nexus)",
    )

    success("NEXUS manifests applied — waiting for pods to start...")
    _wait_for_pods()


@app.command("status")
def status_cmd(
    watch: bool = typer.Option(False, "--watch", "-w", help="Watch continuously"),
    namespace: str = typer.Option("nexus", "--namespace", "-n", help="Kubernetes namespace"),
) -> None:
    """Show status of NEXUS pods and services."""

    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error(f"Failed to get pods in namespace '{namespace}': {result.stderr.strip()}")
        warn("Apply NEXUS manifests first: ppa nexus apply")
        raise typer.Exit(1)

    lines = result.stdout.strip().splitlines()
    table = Table(title=f"NEXUS Pods [dim]({namespace})[/dim]", show_header=True)
    for i, line in enumerate(lines):
        cols = line.split()
        if i == 0:
            for col in cols:
                table.add_column(col)
        else:
            table.add_row(*cols[: len(table.columns)])

    console.print(table)

    # Also show services
    svc_result = subprocess.run(
        ["kubectl", "get", "svc", "-n", namespace],
        capture_output=True,
        text=True,
    )
    if svc_result.returncode == 0:
        console.print()
        console.print("[bold]Services:[/bold]")
        for line in svc_result.stdout.strip().splitlines():
            console.print(f"  {line}")

    if watch:
        console.print("\n[dim]Use Ctrl+C to stop watching[/dim]")
        subprocess.run(["kubectl", "get", "pods", "-n", namespace, "-w"])


@app.command("logs")
def logs_cmd(
    pod: str = typer.Option(None, "--pod", "-p", help="Pod name (default: first nexus-api pod)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream logs"),
    tail: int = typer.Option(50, "--tail", "-n", help="Number of lines to show"),
    namespace: str = typer.Option("nexus", "--namespace", "-n", help="Kubernetes namespace"),
) -> None:
    """Stream nexus-api logs."""
    if pod is None:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace, "-l", "app=nexus-api", "-o", "name"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            error("No nexus-api pod found. Run: ppa nexus apply")
            raise typer.Exit(1)
        pod = result.stdout.strip().splitlines()[0].replace("pod/", "")

    cmd = ["kubectl", "logs", "-n", namespace, pod]
    if follow:
        cmd.append("-f")
    cmd.extend(["--tail", str(tail)])

    subprocess.run(cmd)


@app.command("verify")
def verify_cmd() -> None:
    """Run in-cluster reachability checks for NATS and Prometheus."""
    checks = [
        (
            "NATS health (http://nats.nexus.svc.cluster.local:8222/healthz)",
            ["kubectl", "exec", "deployment/ppa-operator", "--",
             "wget", "-qO-", "http://nats.nexus.svc.cluster.local:8222/healthz"],
        ),
        (
            "Prometheus health (http://prometheus.monitoring:9090/-/healthy)",
            ["kubectl", "exec", "deployment/ppa-operator", "--",
             "wget", "-qO-",
             "http://prometheus-kube-prometheus-prometheus.monitoring:9090/-/healthy"],
        ),
        (
            "nexus-api health (localhost:8080/health — verify this path matches your app)",
            ["kubectl", "exec", "deployment/nexus-api", "-n", "nexus", "--",
             "wget", "-qO-", "http://localhost:8080/health"],
        ),
    ]

    all_pass = True
    for label, cmd in checks:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and ("ok" in result.stdout or "200" in result.stdout):
            success(f"{label.split('(')[0].strip()}: OK")
        else:
            error(f"{label.split('(')[0].strip()}: FAILED")
            console.print(f"  [dim]endpoint:[/dim] {label.split('(', 1)[1].rstrip(')')}")
            console.print(f"  [dim]stdout:[/dim] {result.stdout.strip()}")
            console.print(f"  [dim]stderr:[/dim] {result.stderr.strip()}")
            if "404" in result.stdout or "404" in result.stderr:
                console.print(
                    "  [dim]got 404 — the app's health endpoint may differ.\n"
                    "  try: kubectl exec deploy/nexus-api -n nexus -- wget -qO- http://localhost:8080/ | head -5[/dim]"
                )
            all_pass = False

    if not all_pass:
        warn("Some checks failed — see above")
        raise typer.Exit(1)

    success("All NEXUS in-cluster checks passed")


def _wait_for_pods() -> None:
    """Poll until both nats and nexus-api pods are Running."""
    import time

    watch_pods = ["nats", "nexus-api"]
    console.print("[dim]Waiting for pods to become Running...[/dim]")

    for _ in range(60):  # 60s timeout
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "nexus", "-o", "jsonpath={.items[*].status.phase}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            phases = result.stdout.strip().split()
            if all(p == "Running" for p in phases) and len(phases) >= len(watch_pods):
                success(f"NATS + nexus-api running ({len(phases)} pods)")
                return
        time.sleep(2)

    warn("Pod wait timed out — check status with: ppa nexus status")
