"""Shared Rich helpers — console, spinners, styled output, subprocess wrappers."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from typing import cast

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ppa.config import PPA_THEME, PROMETHEUS_URL, SESSION_FILE

# Singleton Console
console = Console(theme=PPA_THEME, highlight=False)

# Styled output helpers


def success(msg: str) -> None:
    console.print(f"  [success]✓[/success] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [warning]⚠[/warning] {msg}")


def error(msg: str) -> None:
    console.print(f"  [error]✗[/error] {msg}")


def info(msg: str) -> None:
    console.print(f"  [info]ℹ[/info] {msg}")


def heading(title: str) -> None:
    console.print()
    console.rule(f"[heading]{title}[/heading]", style="info")


def step_heading(step_num: int, total: int, title: str) -> None:
    console.print()
    console.print(
        Panel(
            f"[step]STEP {step_num}/{total}[/step]  [bold]{title}[/bold]",
            border_style="step",
            padding=(0, 2),
        )
    )

# Subprocess helpers

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
        error(
            f"Command failed (exit {result.returncode}): {cmd if isinstance(cmd, str) else ' '.join(cmd)}"
        )
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


def kubectl(
    *args: str, namespace: str | None = None, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    """Convenience wrapper around kubectl."""
    cmd = ["kubectl"]
    if namespace:
        cmd += ["-n", namespace]
    cmd += list(args)
    return (
        run_cmd_silent(cmd, check=check)
        if capture
        else run_cmd(cmd, title=f"kubectl {args[0]}", check=check)
    )


def check_binary(name: str) -> bool:
    """Check if a binary is available on PATH."""
    return shutil.which(name) is not None


def wait_for_pods(label: str, namespace: str = "default", timeout: int = 120) -> bool:
    """Wait for pods matching label to be ready."""
    info(f"Waiting for pods: {label} (ns={namespace})")
    result = run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=ready",
            "pod",
            "-l",
            label,
            "-n",
            namespace,
            f"--timeout={timeout}s",
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

# Prometheus helpers

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

# Table builder helper

def build_kv_table(
    title: str, data: dict[str, str], key_style: str = "info", val_style: str = ""
) -> Table:
    """Build a simple key-value Rich table."""
    table = Table(title=title, show_header=False, border_style="info", padding=(0, 2))
    table.add_column("Key", style=key_style, min_width=20)
    table.add_column("Value", style=val_style)
    for k, v in data.items():
        table.add_row(k, str(v))
    return table


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

# Session Management

def save_session(pids: dict[str, int]) -> None:
    """Save background process PIDs and metadata to session file."""
    try:
        data = load_session()
        data["updated_at"] = datetime.now().isoformat()
        if "start_time" not in data:
            data["start_time"] = data["updated_at"]

        if "pids" not in data:
            data["pids"] = {}

        pids_dict = cast(dict[str, object], data["pids"])
        pids_dict.update(pids)

        with open(SESSION_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        warn(f"Failed to save session: {e}")


def load_session() -> dict[str, object]:
    """Load session data from file and migrate old formats."""
    if not SESSION_FILE.exists():
        return {}
    try:
        with open(SESSION_FILE) as f:
            data = cast(dict[str, object], json.load(f))

        # Migration: if data is a flat dict of PIDs, move to "pids" key
        if data and "pids" not in data:
            # Filter out metadata keys if they somehow exist
            pids = {k: v for k, v in data.items() if k not in ["updated_at", "start_time"]}
            meta = {k: v for k, v in data.items() if k in ["updated_at", "start_time"]}
            data = {"pids": pids, **meta}

        return data
    except Exception:
        return {}


def cleanup_session() -> None:
    """Kill all tracked PIDs and remove session file."""
    session = load_session()
    pids = cast(dict[str, int], session.get("pids", {}))
    if not pids:
        info("No active PPA session found.")
        return

    heading("Cleaning up PPA Session")
    for name, pid in pids.items():
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    success(f"Stopped {name}")
                # returncode != 0 means process was already gone — ignore
            else:
                os.kill(pid, 0)  # Raises ProcessLookupError if already dead
                info(f"Stopping {name} (PID: {pid})...")
                os.kill(pid, signal.SIGTERM)
                success(f"Stopped {name}")
        except ProcessLookupError:
            pass  # Process already dead
        except Exception as e:
            warn(f"Could not stop {name}: {e}")

    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
    success("Session cleanup complete.")
