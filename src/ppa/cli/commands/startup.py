"""ppa startup — Cluster bootstrap command (refactored with BaseCommand).

Orchestrates 11-step cluster initialization via startup_steps module.
"""

from __future__ import annotations

import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from ppa.cli.commands.base import BaseCommand
from ppa.cli.commands.startup_steps import (
    STEP_FUNCS,
    STEPS,
    get_app_path,
)
from ppa.cli.utils import console, step_heading
from ppa.config import APP_PORT, GRAFANA_PORT, PROMETHEUS_PORT, get_banner

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


class StartupCommand(BaseCommand):
    """Bootstrap PPA cluster infrastructure (11 steps)."""

    def run(
        self,
        step: int | None = None,
        list_steps: bool = False,
        dry_run: bool = False,
        follow_mode: bool = False,
        app_arg: str | None = None,
    ) -> None:
        """Execute startup procedure.

        Args:
            step: Run only specific step (1-11)
            list_steps: Show available steps and exit
            dry_run: Show plan without executing
            follow_mode: Switch to monitor after startup
            app_arg: Path or git URL to test-app source
        """
        # Handle list_steps early
        if list_steps:
            self._show_step_list()
            raise typer.Exit()

        # Validate and resolve app path
        get_app_path(app_arg)

        # Show startup plan
        steps_to_run = [step] if step else list(range(1, 12))
        self._show_startup_plan(steps_to_run)

        # Handle dry-run
        if dry_run:
            console.print("\n[bold]DRY RUN — Steps to execute[/bold]")
            for s in steps_to_run:
                _, name, desc = STEPS[s - 1]
                description = desc() if callable(desc) else desc
                self.info(f"Step {s}: {name} — {description}")
            raise typer.Exit()

        # Execute steps with progress
        self._execute_steps(steps_to_run, step is not None)

        # Show completion and optional follow mode
        if not step:
            self._show_done_banner()

        if follow_mode:
            from ppa.cli.commands.follow import follow as follow_cmd

            follow_cmd()

    def _show_startup_plan(self, steps: list[int]) -> None:
        """Display plan of steps to execute."""
        console.print(get_banner())
        console.print("\n[bold]Startup Plan:[/]")
        for s in steps:
            step_num, name, desc = STEPS[s - 1]
            description = desc() if callable(desc) else desc
            console.print(f"  [step]{s:02d}[/] [bold]{name}[/] — {description}")
        console.print()

    def _show_step_list(self) -> None:
        """List all available startup steps."""
        console.print(get_banner())
        console.print("\n[bold]Available Steps:[/]")
        for step_num, name, desc in STEPS:
            description = desc() if callable(desc) else desc
            console.print(f"  [step]{step_num:02d}[/] [bold]{name}[/] — {description}")
        console.print()

    def _execute_steps(self, steps_to_run: list[int], single_step: bool) -> None:
        """Execute startup steps with progress tracking."""
        total = len(steps_to_run)
        with Progress(
            SpinnerColumn(style="bright_magenta"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30, style="bright_cyan", complete_style="bright_green"),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[bold]PPA Startup[/bold]", total=total)

            for _i, s in enumerate(steps_to_run, 1):
                _, name, desc = STEPS[s - 1]
                step_heading(s, 11, name)
                progress.update(task, description=f"[bold]Step {s}:[/bold] {name}")

                try:
                    STEP_FUNCS[s]()
                except Exception as e:
                    self.error(f"Step {s} failed: {e}")
                    if single_step:
                        raise typer.Exit(1) from None
                    self.warn("Continuing to next step...")

                progress.advance(task)

    def _show_done_banner(self) -> None:
        """Display completion banner with service URLs."""
        from rich.panel import Panel

        lines = [
            "[success]✓ PPA Infrastructure is Ready[/success]",
            "",
            f"  [info]Prometheus[/info]   → http://localhost:{PROMETHEUS_PORT}",
            f"  [info]Grafana[/info]      → http://localhost:{GRAFANA_PORT}",
            f"  [info]Test App[/info]     → http://localhost:{APP_PORT}",
            "",
            "  [dim]Run [bold]ppa follow[/bold] to switch to live monitoring.[/dim]",
        ]
        console.print()
        console.print(Panel("\n".join(lines), border_style="success", padding=(1, 2)))


@app.callback(invoke_without_command=True)
def startup(
    ctx: typer.Context,
    step: int | None = typer.Option(None, "--step", "-s", help="Run only a specific step (1-11)."),
    list_steps: bool = typer.Option(False, "--list", "-l", help="List all startup steps."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run without executing."),
    follow_mode: bool = typer.Option(
        False, "--follow", "-f", help="Attach to live monitor after startup."
    ),
    app: str | None = typer.Option(None, "--app", "-a", help="Path or git URL to test-app source."),
) -> None:
    """
    [bold]Bootstrap the full PPA cluster infrastructure.[/]

    Runs all 11 steps sequentially: prerequisites → Minikube → Prometheus →
    test-app → traffic-gen → port-forwards → watchdog → verify features →
    CronJob → chaos profiling.

    Use --app to specify test-app source:
        ppa startup --app ./test-app
        ppa startup --app https://github.com/you/test-app.git

    Examples:
        ppa startup                    # Run all 11 steps
        ppa startup --step 5           # Run only step 5 (test-app)
        ppa startup --list             # Show all available steps
        ppa startup --dry-run          # Show plan without executing
    """
    if ctx.invoked_subcommand is not None:
        return

    cmd = StartupCommand()
    cmd.run(step=step, list_steps=list_steps, dry_run=dry_run, follow_mode=follow_mode, app_arg=app)

