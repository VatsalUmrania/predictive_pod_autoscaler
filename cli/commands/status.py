"""ppa status — Cluster health dashboard."""

from __future__ import annotations

import typer
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table

from cli.config import (
    APP_PORT,
    DEFAULT_NAMESPACE,
    GRAFANA_PORT,
    METRICS_PORT,
    PROMETHEUS_PORT,
)
from cli.utils import (
    check_binary,
    console,
    heading,
    prometheus_ready,
    run_cmd_silent,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


def _status_icon(ok: bool) -> str:
    return "[green]🟢[/green]" if ok else "[red]🔴[/red]"


def _check_pod_status(label: str, namespace: str = DEFAULT_NAMESPACE) -> tuple[bool, str]:
    """Check pod status by label selector."""
    result = run_cmd_silent(
        ["kubectl", "get", "pods", "-l", label, "-n", namespace, "--no-headers"],
        check=False,
    )
    if result.returncode != 0:
        return False, "not found"
    lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
    if not lines:
        return False, "no pods"
    # Check if all pods show Running
    all_running = all("Running" in line for line in lines)
    ready_counts = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            ready_counts.append(parts[1])
    status_str = ", ".join(ready_counts) if ready_counts else "?"
    return all_running, status_str


def _check_port(port: int) -> bool:
    """Check if a local port is responding."""
    import requests
    try:
        requests.get(f"http://localhost:{port}", timeout=2)
        return True
    except Exception:
        return False


@app.callback(invoke_without_command=True)
def status(ctx: typer.Context) -> None:
    """
    [bold]Show cluster health status[/] for all PPA components.

    Checks Minikube, pods, port-forwards, Prometheus, and PPA CRs.
    """
    if ctx.invoked_subcommand is not None:
        return

    # ── Infrastructure ────────────────────────────────────────────────
    heading("PPA Cluster Status")

    # Minikube
    mk_result = run_cmd_silent(["minikube", "status", "--format", "{{.Host}}"], check=False)
    mk_running = mk_result.returncode == 0 and "Running" in mk_result.stdout

    infra_table = Table(show_header=False, border_style="bright_cyan", padding=(0, 1))
    infra_table.add_column("Component", style="bold", min_width=20)
    infra_table.add_column("Status", min_width=10)
    infra_table.add_column("Details", style="dim")

    infra_table.add_row("Minikube", _status_icon(mk_running), "Running" if mk_running else "Stopped")

    # Tools
    for tool in ["kubectl", "helm", "docker"]:
        found = check_binary(tool)
        infra_table.add_row(f"  {tool}", _status_icon(found), "installed" if found else "missing")

    console.print()
    console.print(Panel(infra_table, title="[bold]🔧 Infrastructure[/]", border_style="bright_cyan"))

    # ── Pods ──────────────────────────────────────────────────────────
    pods_table = Table(show_header=True, border_style="bright_cyan", padding=(0, 1), header_style="bold")
    pods_table.add_column("Pod", style="bold", min_width=20)
    pods_table.add_column("Status", min_width=6)
    pods_table.add_column("Ready", style="dim")

    pod_checks = [
        ("PPA Operator", "app=ppa-operator", DEFAULT_NAMESPACE),
        ("Test App", "app=test-app", DEFAULT_NAMESPACE),
        ("Traffic Gen", "app=traffic-gen", DEFAULT_NAMESPACE),
        ("Prometheus", "app.kubernetes.io/name=prometheus", "monitoring"),
        ("Grafana", "app.kubernetes.io/name=grafana", "monitoring"),
    ]

    for name, label, ns in pod_checks:
        ok, details = _check_pod_status(label, ns)
        pods_table.add_row(name, _status_icon(ok), details)

    console.print()
    console.print(Panel(pods_table, title="[bold]📦 Pods[/]", border_style="bright_cyan"))

    # ── Port Forwards ─────────────────────────────────────────────────
    ports_table = Table(show_header=True, border_style="bright_cyan", padding=(0, 1), header_style="bold")
    ports_table.add_column("Service", style="bold", min_width=14)
    ports_table.add_column("Port", justify="right", min_width=6)
    ports_table.add_column("Status", min_width=6)

    port_checks = [
        ("Prometheus", PROMETHEUS_PORT),
        ("Grafana", GRAFANA_PORT),
        ("Test App", APP_PORT),
        ("Metrics", METRICS_PORT),
    ]

    for name, port in port_checks:
        ok = _check_port(port)
        ports_table.add_row(name, str(port), _status_icon(ok))

    # Prometheus deep check
    prom_ok = prometheus_ready()

    console.print()
    console.print(Panel(ports_table, title="[bold]🔌 Port Forwards[/]", border_style="bright_cyan"))

    # ── PPA Custom Resources ──────────────────────────────────────────
    cr_result = run_cmd_silent(
        ["kubectl", "get", "ppa", "--all-namespaces", "-o", "wide", "--no-headers"],
        check=False,
    )

    if cr_result.returncode == 0 and cr_result.stdout.strip():
        cr_table = Table(border_style="bright_magenta", padding=(0, 1), header_style="bold")
        cr_table.add_column("Name", style="bold")
        cr_table.add_column("Namespace")
        cr_table.add_column("Details", style="dim")

        for line in cr_result.stdout.strip().splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 2:
                cr_table.add_row(parts[1] if len(parts) > 1 else parts[0], parts[0], parts[2] if len(parts) > 2 else "")

        console.print()
        console.print(Panel(cr_table, title="[bold]🤖 PredictiveAutoscaler CRs[/]", border_style="bright_magenta"))
    else:
        console.print()
        console.print(Panel("[dim]No PredictiveAutoscaler CRs found[/dim]", title="[bold]🤖 PPA CRs[/]", border_style="dim"))

    # ── Summary ───────────────────────────────────────────────────────
    console.print()
    if mk_running and prom_ok:
        console.print("[success]✔[/success] Cluster is healthy")
    elif mk_running:
        console.print("[warning]⚠[/warning] Cluster is running but Prometheus is not reachable")
    else:
        console.print("[error]✘[/error] Minikube is not running")
