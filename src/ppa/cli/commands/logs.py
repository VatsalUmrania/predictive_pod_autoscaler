"""ppa logs — Unified log viewer for operator, test-app, and traffic-gen.

Merges functionality from the old follow.py and toolbox.py logs command.
"""

from __future__ import annotations

import subprocess

import typer

from ppa.cli.utils import (
    console,
    error_block,
    info,
    success,
)
from ppa.config import DEFAULT_NAMESPACE

# Component name → kubectl target mapping
COMPONENT_MAP = {
    "operator": "deployment/ppa-operator",
    "test-app": "deployment/test-app",
    "traffic": "deployment/traffic-gen",
}


def logs_cmd(
    component: str = typer.Argument(
        "operator", help="Component to view (operator, test-app, traffic)."
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Stream logs in real-time."
    ),
    tail: int = typer.Option(
        100, "--tail", "-n", help="Number of lines to show."
    ),
    namespace: str = typer.Option(
        DEFAULT_NAMESPACE, "--namespace", help="Kubernetes namespace."
    ),
) -> None:
    """View logs from PPA components.

    \b
    EXAMPLES
      ppa logs                    # operator logs (last 100 lines)
      ppa logs --follow           # stream operator logs live
      ppa logs test-app -n 50     # last 50 lines from test-app
      ppa logs traffic -f         # stream traffic-gen logs

    \b
    COMPONENTS
      operator   (default)  The PPA operator deployment
      test-app              The target application
      traffic               The traffic generator
    """
    target = COMPONENT_MAP.get(component.lower())
    if not target:
        error_block(
            f"Unknown component: {component}",
            cause=f"Valid components: {', '.join(COMPONENT_MAP.keys())}",
            fix=f"ppa logs {list(COMPONENT_MAP.keys())[0]}",
        )
        raise typer.Exit(1)

    cmd = ["kubectl", "logs"]
    if follow:
        cmd.append("-f")
    cmd.extend([f"--tail={tail}", target, "-n", namespace])

    console.print()
    if follow:
        info(f"Streaming {component} logs...  [dim]Ctrl+C to stop[/]")
    else:
        info(f"Last {tail} lines from {component}")
    console.print()

    try:
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0 and not follow:
            error_block(
                f"Failed to fetch logs from {component}",
                cause=f"kubectl returned exit code {result.returncode}",
                fix=f"kubectl get pods -n {namespace} -l app={component.replace('test-app', 'test-app')}",
            )
    except KeyboardInterrupt:
        console.print()
        success("Log stream stopped.")
