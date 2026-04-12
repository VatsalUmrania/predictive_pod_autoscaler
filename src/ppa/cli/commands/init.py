"""ppa init — Bootstrap cluster infrastructure.

Replaces `ppa startup`. Orchestrates 11-step cluster initialization
via the startup_steps module. All business logic is delegated to existing
step functions — this file handles only the CLI layer.
"""

from __future__ import annotations

import typer

from ppa.cli.commands import startup_steps
from ppa.cli.commands.startup_steps import STEP_FUNCS, STEPS, get_app_path
from ppa.cli.utils import (
    console,
    error_block,
    next_step,
    step_heading,
    success,
    warn,
)
from ppa.config import APP_PORT, GRAFANA_PORT, PROMETHEUS_PORT


def init_cmd(
    step: int | None = typer.Option(
        None, "--step", "-s", help="Run only a specific step (1–11)."
    ),
    list_steps: bool = typer.Option(
        False, "--list", "-l", help="List all startup steps without running."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would run, no execution."
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Attach to live dashboard after completion."
    ),
    app: str | None = typer.Option(
        None, "--app", "-a", help="Path or git URL to test-app source."
    ),
) -> None:
    """Bootstrap cluster infrastructure.

    Runs all 11 steps sequentially: prerequisites → Minikube → Prometheus →
    test-app → traffic-gen → port-forwards → verify.

    \b
    EXAMPLES
      ppa init                    # Run all 11 steps
      ppa init --step 5           # Run only step 5
      ppa init --list             # Show all available steps
      ppa init --dry-run          # Show plan without executing
      ppa init --follow           # Bootstrap then attach to dashboard

    \b
    REQUIRES
      • kubectl and Docker installed locally
      • Network connectivity for Helm chart pulls
    """
    # List steps
    if list_steps:
        console.print()
        console.print("  [bold]Available Steps[/]")
        console.print()
        for step_num, name, desc in STEPS:
            description = desc() if callable(desc) else desc
            console.print(f"  [bold]{step_num:>2}[/]   {name}  [dim]— {description}[/]")
        console.print()
        raise typer.Exit()

    # Resolve app path
    app_path = get_app_path(app)
    startup_steps._app_path = app_path

    steps_to_run = [step] if step else list(range(1, 12))

    # Dry run
    if dry_run:
        console.print()
        console.print("  [bold]Dry run — steps that would execute:[/]")
        console.print()
        for s in steps_to_run:
            _, name, desc = STEPS[s - 1]
            description = desc() if callable(desc) else desc
            console.print(f"  [bold]{s:>2}[/]   {name}  [dim]— {description}[/]")
        console.print()
        console.print("  To execute:  [bold]ppa init[/]")
        raise typer.Exit()

    # Execute steps
    console.print()
    console.print("  Bootstrapping cluster infrastructure...")
    console.print()

    total = len(steps_to_run)
    for s in steps_to_run:
        _, name, _desc = STEPS[s - 1]
        step_heading(s, 11, name)

        try:
            STEP_FUNCS[s]()
            success(f"{name}")
        except Exception as e:
            error_block(
                f"Step {s} failed: {name}",
                cause=str(e),
                fix=f"ppa init --step {s}",
            )
            if step is not None:
                raise typer.Exit(1) from None
            warn("Continuing to next step...")

    # Success summary
    if step is None:
        console.print()
        success(f"Infrastructure ready.  {total}/{total} steps completed.")
        console.print()
        console.print(f"     [bold]prometheus[/]   http://localhost:{PROMETHEUS_PORT}")
        console.print(f"     [bold]grafana[/]      http://localhost:{GRAFANA_PORT}")
        console.print(f"     [bold]test-app[/]     http://localhost:{APP_PORT}")
        next_step(
            "ppa add --app-name myapp --target myapp-deployment",
            "register an app for autoscaling",
        )

    # Follow mode
    if follow:
        from ppa.cli.commands.watch import watch_cmd

        watch_cmd(interval=15, app=None)
