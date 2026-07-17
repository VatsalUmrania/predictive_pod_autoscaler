"""Console + minimal output helpers used by `ppa model push`."""

from __future__ import annotations

import subprocess

from rich.console import Console

from ppa.config import PPA_THEME

console = (
    Console(theme=PPA_THEME, highlight=False) if PPA_THEME else Console(highlight=False)
)


def success(msg: str) -> None:
    console.print(f"  [bold green]✓[/]  {msg}")


def warn(msg: str) -> None:
    console.print(f"  [bold yellow]⚠[/]  {msg}")


def error(msg: str) -> None:
    console.print(f"  [bold red]✗[/]  {msg}")


def info(msg: str) -> None:
    console.print(f"  [cyan]{msg}[/]")


def heading(title: str) -> None:
    console.print()
    console.rule(f"[heading]{title}[/heading]", style="info")


def get_minikube_docker_env() -> dict[str, str]:
    """Return Docker env vars from minikube without shell eval (cross-platform)."""
    result = subprocess.run(
        ["minikube", "docker-env", "--shell", "none"],
        capture_output=True,
        text=True,
        check=False,
    )
    env_vars: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env_vars[key.strip()] = value.strip().strip('"').strip("'")
    return env_vars
