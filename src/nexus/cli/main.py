"""
NEXUS CLI

Usage:
    nexus server start [--nats-url=URL] [--port=PORT] [--prometheus-url=URL]
    nexus approve <action_id> [--api-url=URL]   # approve a staged human-review action
    nexus reject  <action_id> [--api-url=URL]   # reject  a staged human-review action
    nexus --version
"""

from __future__ import annotations

import logging
import os
import sys

import click

from nexus.cli.server import server

try:
    from rich.console import Console

    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


@click.group()
@click.version_option(package_name="predictive_pod_autoscaler")
def cli() -> None:
    """NEXUS self-healing infrastructure CLI."""
    pass


cli.add_command(server)


def _api_url(ctx_api_url: str | None) -> str:
    raw = ctx_api_url or os.environ.get("NEXUS_API_URL", "http://localhost:8080")
    return raw.rstrip("/")


def _post_approval(path: str, action_id: str, api_url: str) -> None:
    import httpx

    url = f"{api_url}{path.format(action_id=action_id)}"
    try:
        resp = httpx.post(url, timeout=10.0)
    except httpx.HTTPError as exc:
        if _RICH:
            console.print(f"[red]Could not reach NEXUS API at {api_url}: {exc}[/red]")
        else:
            print(f"Could not reach NEXUS API at {api_url}: {exc}")
        sys.exit(2)

    if resp.status_code == 404:
        if _RICH:
            console.print(f"[red]Action {action_id} not found in approval queue.[/red]")
        else:
            print(f"Action {action_id} not found in approval queue.")
        sys.exit(1)
    if resp.status_code == 503:
        if _RICH:
            console.print("[red]NEXUS server reported 503 — approval queue not accessible.[/red]")
        else:
            print("NEXUS server reported 503 — approval queue not accessible.")
        sys.exit(1)
    resp.raise_for_status()
    body = resp.json()
    status = body.get("status", "?")
    outcome = body.get("outcome")
    if _RICH:
        console.print(f"[green]Action {action_id} → {status}[/green]")
        if outcome:
            console.print(f"  outcome: {outcome.get('status')} (audit {outcome.get('action_id')})")
    else:
        print(f"Action {action_id} → {status}")
        if outcome:
            print(f"  outcome: {outcome.get('status')} (audit {outcome.get('action_id')})")


@cli.command()
@click.argument("action_id")
@click.option(
    "--api-url",
    default=None,
    help="NEXUS status API base URL (env: NEXUS_API_URL, default http://localhost:8080)",
)
def approve(action_id: str, api_url: str | None) -> None:
    """Approve a pending human-review action and dispatch it through governance."""
    _post_approval("/approve/{action_id}", action_id, _api_url(api_url))


@cli.command()
@click.argument("action_id")
@click.option(
    "--api-url",
    default=None,
    help="NEXUS status API base URL (env: NEXUS_API_URL, default http://localhost:8080)",
)
def reject(action_id: str, api_url: str | None) -> None:
    """Reject a pending human-review action."""
    _post_approval("/reject/{action_id}", action_id, _api_url(api_url))


def main() -> int:
    """Entry point registered as the `nexus` console script."""
    try:
        cli()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.exception("CLI error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
