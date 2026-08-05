"""
NEXUS CLI

Usage:
    nexus server start [--nats-url=URL] [--port=PORT] [--prometheus-url=URL]
    nexus approve <action_id> [--api-url=URL]   # approve a staged human-review action
    nexus reject  <action_id> [--api-url=URL]   # reject  a staged human-review action
    nexus --version
"""

from __future__ import annotations

import os
import sys

try:
    from rich.console import Console

    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

import asyncio
import logging
from typing import NoReturn

import click

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


def _get_env_or_default(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


@cli.group()
def server() -> None:
    """NEXUS server lifecycle commands."""
    pass


@server.command()
@click.option(
    "--nats-url",
    default=lambda: _get_env_or_default("NATS_URL", "nats://localhost:4222"),
    show_default=False,
    help="NATS JetStream broker URL",
)
@click.option(
    "--port",
    default=8080,
    show_default=True,
    help="Port for the FastAPI status API",
)
@click.option(
    "--prometheus-url",
    default=lambda: _get_env_or_default("PROMETHEUS_URL", "http://prometheus:9090"),
    show_default=False,
    help="Prometheus URL for metrics scraping",
)
@click.option(
    "--runbook-dir",
    default=None,
    help="Override path to runbook YAML files",
)
@click.option(
    "--gemini-api-key",
    default=lambda: os.environ.get("NEXUS_LLM_API_KEY"),
    help="Gemini API key for LLM-powered RCA",
)
@click.option(
    "--log-level",
    default=lambda: os.environ.get("NEXUS_LOG_LEVEL")
    or os.environ.get("LOG_LEVEL", "INFO"),
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging level (NEXUS_LOG_LEVEL env var overrides)",
)
def start(
    nats_url: str,
    port: int,
    prometheus_url: str,
    runbook_dir: str | None,
    gemini_api_key: str | None,
    log_level: str,
) -> NoReturn:
    """Start the NEXUS server (NexusServer)."""
    logging.getLogger().setLevel(getattr(logging, log_level.upper()))

    async def _run() -> NoReturn:
        from nexus.server import NexusServer

        nats_url_val = nats_url() if callable(nats_url) else nats_url
        prom_url_val = prometheus_url() if callable(prometheus_url) else prometheus_url
        gemini_key = gemini_api_key() if callable(gemini_api_key) else gemini_api_key

        if _RICH:
            console.print(
                f"[bold green]Starting NexusServer[/bold green]  NATS={nats_url_val}"
            )
            console.print(f"  Prometheus={prom_url_val}  Status API={port}")

        try:
            server = await NexusServer.create(
                nats_url=nats_url_val,
                runbook_dir=runbook_dir,
                prometheus_url=prom_url_val,
                gemini_api_key=gemini_key,
                status_port=port,
            )
            await server.start()

            if _RICH:
                console.print("[bold green] NexusServer running[/bold green]")
                console.print(f"  Status API: http://0.0.0.0:{port}")
                console.print("  Press Ctrl+C to stop")

            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            if _RICH:
                console.print("\n[yellow]Received interrupt — shutting down[/yellow]")
            sys.exit(0)
        except Exception as exc:
            log.exception("NexusServer failed to start: %s", exc)
            sys.exit(1)

    asyncio.run(_run())


# ── Approval commands ─────────────────────────────────────────────────────────
# These hit the status API's /approve/{id} and /reject/{id} endpoints, which
# re-dispatch the action through the governance plane (see status_api.py).
# Default URL matches the nexus-api deployment; override with NEXUS_API_URL.


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
