"""Shared Rich helpers — console, spinners, styled output, subprocess wrappers."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.config import PPA_THEME, PROMETHEUS_URL

# ── Singleton Console ────────────────────────────────────────────────────────
console = Console(theme=PPA_THEME, highlight=False)


# ── Styled output helpers ────────────────────────────────────────────────────

def success(msg: str) -> None:
    console.print(f"  [success]✔[/success] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [warning]⚠[/warning] {msg}")


def error(msg: str) -> None:
    console.print(f"  [error]✘[/error] {msg}")


def info(msg: str) -> None:
    console.print(f"  [info]→[/info] {msg}")


def heading(title: str) -> None:
    console.print()
    console.rule(f"[heading]{title}[/heading]", style="bright_cyan")


def step_heading(step_num: int, total: int, title: str) -> None:
    console.print()
    console.print(
        Panel(
            f"[step]STEP {step_num}/{total}[/step]  [bold]{title}[/bold]",
            border_style="bright_magenta",
            padding=(0, 2),
        )
    )


# ── Subprocess helpers ───────────────────────────────────────────────────────

def run_cmd(
    cmd: list[str] | str,
    title: str = "Running",
    *,
    capture: bool = False,
    check: bool = True,
    cwd: str | None = None,
    env: dict | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess with a Rich spinner.

    Returns the CompletedProcess result. Raises on non-zero exit if check=True.
    """
    with console.status(f"[info]{title}...[/info]", spinner="dots"):
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
            cwd=cwd,
            env=env,
            shell=shell,
        )

    if check and result.returncode != 0:
        error(f"Command failed (exit {result.returncode}): {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
        if result.stderr:
            console.print(f"    [dim]{result.stderr.strip()[:500]}[/dim]")
        sys.exit(result.returncode)

    return result


def run_cmd_silent(
    cmd: list[str] | str,
    *,
    check: bool = True,
    cwd: str | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess silently (capture all output). Returns CompletedProcess."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        shell=shell,
    )
    if check and result.returncode != 0:
        error(f"Command failed: {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
        if result.stderr:
            console.print(f"    [dim]{result.stderr.strip()[:300]}[/dim]")
        sys.exit(result.returncode)
    return result


def kubectl(*args: str, namespace: str | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Convenience wrapper around kubectl."""
    cmd = ["kubectl"]
    if namespace:
        cmd += ["-n", namespace]
    cmd += list(args)
    return run_cmd_silent(cmd, check=check) if capture else run_cmd(cmd, title=f"kubectl {args[0]}", check=check)


def check_binary(name: str) -> bool:
    """Check if a binary is available on PATH."""
    return shutil.which(name) is not None


def wait_for_pods(label: str, namespace: str = "default", timeout: int = 120) -> bool:
    """Wait for pods matching label to be ready."""
    info(f"Waiting for pods: {label} (ns={namespace})")
    result = run_cmd(
        [
            "kubectl", "wait", "--for=condition=ready", "pod",
            "-l", label, "-n", namespace, f"--timeout={timeout}s",
        ],
        title=f"Waiting for {label}",
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        success("Pods ready")
        return True
    warn("Timeout — check pods manually")
    return False


# ── Prometheus helpers ───────────────────────────────────────────────────────

def query_prometheus(query: str, url: str = PROMETHEUS_URL) -> str | None:
    """Run an instant query against Prometheus. Returns the scalar value or None."""
    import requests
    try:
        resp = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return str(round(float(results[0]["value"][1]), 2))
    except Exception:
        pass
    return None


def prometheus_ready(url: str = PROMETHEUS_URL) -> bool:
    """Check if Prometheus is reachable."""
    import requests
    try:
        resp = requests.get(f"{url}/-/ready", timeout=5)
        return "Ready" in resp.text
    except Exception:
        return False


# ── Table builder helper ─────────────────────────────────────────────────────

def build_kv_table(title: str, data: dict[str, str], key_style: str = "info", val_style: str = "") -> Table:
    """Build a simple key-value Rich table."""
    table = Table(title=title, show_header=False, border_style="bright_cyan", padding=(0, 2))
    table.add_column("Key", style=key_style, min_width=20)
    table.add_column("Value", style=val_style)
    for k, v in data.items():
        table.add_row(k, str(v))
    return table
