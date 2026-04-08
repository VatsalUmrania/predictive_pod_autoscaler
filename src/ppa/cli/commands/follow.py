"""ppa follow — Attach to a running session and show live logs/health."""

from __future__ import annotations

import time
from datetime import datetime

import typer
from rich.live import Live
from rich.table import Table

from ppa.cli.utils import (
    cleanup_session,
    console,
    load_session,
)
from ppa.config import (
    APP_PORT,
    GRAFANA_PORT,
    METRICS_PORT,
    PROMETHEUS_PORT,
    get_banner,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


def _check_port(port: int) -> bool:
    import requests

    try:
        requests.get(f"http://localhost:{port}", timeout=0.5)
        return True
    except Exception:
        return False


def _build_follow_table(session: dict) -> Table:
    table = Table(show_header=True, header_style="bold cyan", box=None)
    table.add_column("Service", style="bold")
    table.add_column("Port", justify="right")
    table.add_column("Status", justify="center")

    # Calculate uptime
    start_time_str = session.get("start_time")
    uptime = "Unknown"
    if start_time_str:
        try:
            start_time = datetime.fromisoformat(start_time_str)
            delta = datetime.now() - start_time
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime = f"{hours}h {minutes}m {seconds}s"
        except Exception:
            pass

    services = [
        ("Prometheus", PROMETHEUS_PORT),
        ("Grafana", GRAFANA_PORT),
        ("Test App", APP_PORT),
        ("Metrics", METRICS_PORT),
    ]

    for name, port in services:
        ok = _check_port(port)
        status = "[green]● ONLINE[/green]" if ok else "[red]○ OFFLINE[/red]"
        table.add_row(name, str(port), status)

    table.caption = f"[dim]Session Uptime: {uptime}[/dim]"
    return table


@app.callback(invoke_without_command=True)
def follow():
    """
    [bold]Attach to a running PPA session[/] and show live status.

    Automatically cleans up services when you exit with Ctrl+C.
    """
    session = load_session()
    if not session:
        console.print(
            "[error]✗[/error] No active PPA session found. Run [bold]ppa startup[/bold] first."
        )
        raise typer.Exit(1)

    console.print(get_banner())
    console.print("\n[bold]Live Session Monitor[/]")
    console.print("[dim]Press [bold]Ctrl+C[/bold] to stop services and exit.[/dim]")
    console.print()

    try:
        with Live(_build_follow_table(session), refresh_per_second=2, console=console) as live:
            while True:
                live.update(_build_follow_table(session))
                time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[info]Stopping session...[/info]")
        cleanup_session()
        console.print("[success]✓[/success] Session cleaned up.")
