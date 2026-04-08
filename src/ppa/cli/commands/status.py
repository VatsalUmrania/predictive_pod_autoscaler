"""ppa status — Cluster health dashboard."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from ppa.cli.utils import (
    check_binary,
    console,
    prometheus_ready,
    run_cmd_silent,
)
from ppa.config import (
    APP_PORT,
    DEFAULT_NAMESPACE,
    GRAFANA_PORT,
    METRICS_PORT,
    PROMETHEUS_PORT,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


def _status_icon(ok: bool) -> str:
    return "[success]✓[/success]" if ok else "[error]✗[/error]"


def _check_pod_status(label: str, namespace: str = DEFAULT_NAMESPACE) -> tuple[bool, str]:
    """Check pod status by label selector."""
    result = run_cmd_silent(
        ["kubectl", "get", "pods", "-l", label, "-n", namespace, "--no-headers"],
        check=False,
    )
    if result.returncode != 0:
        return False, "not found"
    lines_list = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if not lines_list:
        return False, "no pods"
    # Check if all pods show Running
    all_running = all("Running" in line for line in lines_list)
    ready_counts = []
    for line in lines_list:
        parts = line.split()
        if len(parts) >= 2:
            ready_counts.append(parts[1])
    status_str = ", ".join(ready_counts) if ready_counts else "?"
    return all_running, status_str


def _check_port(port: int) -> bool:
    """Check if a local port is responding."""
    import requests

    try:
        requests.get(f"http://localhost:{port}", timeout=0.5)
        return True
    except Exception:
        return False


def _build_infra_panel() -> Panel:
    """Infrastructure status table."""
    mk_result = run_cmd_silent(["minikube", "status", "--format", "{{.Host}}"], check=False)
    mk_running = mk_result.returncode == 0 and "Running" in mk_result.stdout

    table = Table(show_header=False, border_style="info", padding=(0, 1), box=None)
    table.add_column("Component", style="bold", min_width=16)
    table.add_column("Status", min_width=6)
    table.add_column("Details", justify="right", style="italic")

    table.add_row("Minikube", _status_icon(mk_running), "Running" if mk_running else "Stopped")

    for tool in ["kubectl", "helm", "docker"]:
        found = check_binary(tool)
        table.add_row(f"  {tool}", _status_icon(found), "OK" if found else "MISSING")

    return Panel(table, title="[bold]Infrastructure[/]", border_style="info")


def _build_ports_panel() -> Panel:
    """Connectivity / Port Forwards status table."""
    table = Table(
        show_header=True,
        border_style="info",
        padding=(0, 1),
        header_style="heading",
        box=None,
    )
    table.add_column("Service", style="bold", min_width=12)
    table.add_column("Port", justify="right", min_width=6)
    table.add_column("Status", justify="center", min_width=6)

    ports = [
        ("Prometheus", PROMETHEUS_PORT),
        ("Grafana", GRAFANA_PORT),
        ("Test App", APP_PORT),
        ("Metrics", METRICS_PORT),
    ]

    for name, port in ports:
        ok = _check_port(port)
        table.add_row(name, str(port), _status_icon(ok))

    return Panel(table, title="[bold]Connectivity[/]", border_style="info")


def _build_pods_panel() -> Panel:
    """Pod health status table."""
    table = Table(show_header=True, border_style="info", padding=(0, 1), header_style="heading")
    table.add_column("Pod Group", style="bold", min_width=20)
    table.add_column("Status", justify="center", min_width=8)
    table.add_column("Ready Replicas", justify="right", style="italic")

    checks = [
        ("PPA Operator", "app=ppa-operator", DEFAULT_NAMESPACE),
        ("Test App", "app=test-app", DEFAULT_NAMESPACE),
        ("Traffic Gen", "app=traffic-gen", DEFAULT_NAMESPACE),
        ("Prometheus", "app.kubernetes.io/name=prometheus", "monitoring"),
        ("Grafana", "app.kubernetes.io/name=grafana", "monitoring"),
    ]

    for name, label, ns in checks:
        ok, details = _check_pod_status(label, ns)
        table.add_row(name, _status_icon(ok), details)

    return Panel(table, title="[bold]Pod Health[/]", border_style="info")


def _build_cr_panel() -> Panel:
    """PPA Custom Resources status table."""
    cr_result = run_cmd_silent(
        ["kubectl", "get", "ppa", "--all-namespaces", "-o", "wide", "--no-headers"],
        check=False,
    )

    if not cr_result.stdout.strip():
        return Panel(
            "[italic]No PredictiveAutoscaler CRs found[/italic]",
            title="[bold]PPA Resources[/]",
            border_style="step",
        )

    table = Table(border_style="step", padding=(0, 1), header_style="heading")
    table.add_column("Name", style="bold", min_width=20)
    table.add_column("Deployment", min_width=12)
    table.add_column("Min", justify="center")
    table.add_column("Max", justify="center")
    table.add_column("Predicted Load", justify="right", no_wrap=True)
    table.add_column("Last Scale", justify="right", no_wrap=True)

    for line in cr_result.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 8:
            # NAMESPACE[0] NAME[1] DEPLOY[2] CONTAINER[3] MIN[4] MAX[5] LOAD[6] SCALE[7]
            table.add_row(
                parts[1],
                parts[2],
                parts[4],
                parts[5],
                f"[metric]{parts[6]}[/metric]",
                parts[7],
            )
        elif len(parts) >= 2:
            table.add_row(parts[1], parts[0], "-", "-", "-", "-")

    return Panel(table, title="[bold]PredictiveAutoscaler Resources[/]", border_style="step")


@app.callback(invoke_without_command=True)
def status(ctx: typer.Context) -> None:
    """
    [bold]Show cluster health status dashboard[/] for all PPA components.
    """
    if ctx.invoked_subcommand is not None:
        return

    from cli.config import get_banner

    console.print(get_banner())
    console.print("\n[bold]Cluster Status Summary[/]")

    # Build components
    infra = _build_infra_panel()
    ports = _build_ports_panel()
    pods = _build_pods_panel()
    crs = _build_cr_panel()

    # Layout for combined Infrastructure and Ports
    top_grid = Table.grid(expand=True)
    top_grid.add_column(ratio=1)
    top_grid.add_column(ratio=1)
    top_grid.add_row(infra, ports)

    console.print()
    console.print(top_grid)
    console.print()
    console.print(pods)
    console.print()
    console.print(crs)

    # Summary logic (remain same but more compact)
    # Re-run quick checks for summary
    mk_running = run_cmd_silent(["minikube", "status"], check=False).returncode == 0
    prom_ok = prometheus_ready()

    console.print()
    if mk_running and prom_ok:
        console.print("[success]✓[/success] Cluster is healthy")
    elif mk_running:
        console.print("[warning]⚠[/warning] Cluster is running but Prometheus is not reachable")
    else:
        console.print("[error]✗[/error] Minikube is not running")
