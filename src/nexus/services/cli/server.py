"""NEXUS server lifecycle commands."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, NoReturn

import click

try:
    from rich.console import Console

    _RICH = True
    console: Any = Console()
except ImportError:
    _RICH = False
    console = None

log = logging.getLogger(__name__)


def _get_env_or_default(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


@click.group()
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
        from nexus.server_bootstrap import NexusServer

        nats_url_val = nats_url() if callable(nats_url) else nats_url
        prom_url_val = prometheus_url() if callable(prometheus_url) else prometheus_url
        gemini_key = gemini_api_key() if callable(gemini_api_key) else gemini_api_key

        if _RICH:
            console.print(
                f"[bold green]Starting NexusServer[/bold green]  NATS={nats_url_val}"
            )
            console.print(f"  Prometheus={prom_url_val}  Status API={port}")

        try:
            srv = await NexusServer.create(
                nats_url=nats_url_val,
                runbook_dir=runbook_dir,
                prometheus_url=prom_url_val,
                gemini_api_key=gemini_key,
                status_port=port,
            )
            await srv.start()

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
