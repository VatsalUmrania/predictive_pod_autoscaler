"""ppa deploy — Train → Convert → Deploy operator (refactored with BaseCommand).

Orchestrates multi-stage operator deployment using stage modules.
"""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from ppa.cli.commands.base import BaseCommand
from ppa.cli.commands.deploy_stages import (
    build_and_deploy,
    handle_hpa,
    retrain_lstm,
    tail_logs,
)
from ppa.cli.utils import console
from ppa.config import (
    ARTIFACTS_DIR,
    CHAMPION_DIR,
    DEFAULT_APP_NAME,
    DEFAULT_CSV,
    DEFAULT_EPOCHS,
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


class DeployCommand(BaseCommand):
    """Deploy PPA operator with optional retraining."""

    def run(
        self,
        app_name: str = DEFAULT_APP_NAME,
        retrain: bool = False,
        horizon: str = DEFAULT_HORIZON,
        csv: str = DEFAULT_CSV,
        epochs: int = DEFAULT_EPOCHS,
        lookback: int = DEFAULT_LOOKBACK,
        patience: int = 20,
        skip_build: bool = False,
        delete_hpa: bool | None = None,
        no_watch: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Execute deployment pipeline.

        Args:
            app_name: Target application name
            retrain: Whether to retrain models before deploying
            horizon: Prediction horizon target column
            csv: Path to training CSV
            epochs: Number of training epochs
            lookback: Lookback window steps
            patience: Early stopping patience
            skip_build: Skip Docker image rebuild
            delete_hpa: Delete or keep existing HPA
            no_watch: Don't tail operator logs after deploy
            dry_run: Show planned steps without executing
        """
        artifacts_dir = str(ARTIFACTS_DIR)
        champion_dir = str(CHAMPION_DIR / app_name / horizon)

        # Display plan
        self._show_plan(app_name, horizon, retrain, skip_build, csv, dry_run)

        if dry_run:
            raise typer.Exit()

        # Execute deployment
        self._execute_deployment(
            app_name=app_name,
            retrain=retrain,
            horizon=horizon,
            csv=csv,
            epochs=epochs,
            lookback=lookback,
            patience=patience,
            skip_build=skip_build,
            delete_hpa=delete_hpa,
            artifacts_dir=artifacts_dir,
            champion_dir=champion_dir,
        )

        # Tail logs
        if not no_watch:
            tail_logs(no_watch=False)

    def _show_plan(
        self,
        app_name: str,
        horizon: str,
        retrain: bool,
        skip_build: bool,
        csv: str,
        dry_run: bool,
    ) -> None:
        """Display deployment plan."""
        banner_data = {
            "App": app_name,
            "Horizon": horizon,
            "Retrain": str(retrain),
            "Skip build": str(skip_build),
            "CSV": csv,
        }
        table = Table(show_header=False, border_style="magenta", padding=(0, 2))
        table.add_column("Key", style="info")
        table.add_column("Value")
        for k, v in banner_data.items():
            table.add_row(k, v)
        console.print()
        with console.status("[bold bright_cyan]PPA Deploy[/]", spinner="clock"):
            console.print(
                Panel(
                    table,
                    title="[bold bright_cyan]PPA Deploy[/]",
                    border_style="bright_cyan",
                )
            )

        if dry_run:
            console.print("\n[bold]DRY RUN — Planned steps[/bold]")
            steps = ["Retrain LSTM", "Convert → TFLite", "Promote artifacts"] if retrain else []
            steps += [
                "Handle HPA",
                "Scale down operator",
                "Build Docker image",
                "Push models to PVC",
                "Deploy operator",
                "Apply CR",
            ]
            for i, s in enumerate(steps, 1):
                self.info(f"Step {i}: {s}")

    def _execute_deployment(
        self,
        app_name: str,
        retrain: bool,
        horizon: str,
        csv: str,
        epochs: int,
        lookback: int,
        patience: int,
        skip_build: bool,
        delete_hpa: bool | None,
        artifacts_dir: str,
        champion_dir: str,
    ) -> None:
        """Execute the full deployment pipeline."""
        total_steps = 9 if retrain else 6
        current = 0

        with Progress(
            SpinnerColumn(style="bright_magenta"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30, style="bright_cyan", complete_style="bright_green"),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[bold]PPA Deploy[/bold]", total=total_steps)

            # Retrain + Convert + Promote (if enabled)
            if retrain:
                current = retrain_lstm(
                    progress=progress,
                    task=task,
                    current=current,
                    total_steps=total_steps,
                    app_name=app_name,
                    csv=csv,
                    horizon=horizon,
                    lookback=lookback,
                    epochs=epochs,
                    patience=patience,
                    artifacts_dir=artifacts_dir,
                )

            # Verify champion dir exists
            import os

            if not os.path.isdir(champion_dir):
                self.error(
                    f"Champion dir not found: {champion_dir}\nRun with --retrain or train manually first."
                )
                raise typer.Exit(1)

            # Handle HPA and scale operator
            current = handle_hpa(
                progress=progress,
                task=task,
                current=current,
                total_steps=total_steps,
                app_name=app_name,
                delete_hpa=delete_hpa,
            )

            # Build and deploy
            build_and_deploy(
                progress=progress,
                task=task,
                current=current,
                total_steps=total_steps,
                skip_build=skip_build,
                lookback=lookback,
            )


@app.callback(invoke_without_command=True)
def deploy(
    ctx: typer.Context,
    app_name: str = typer.Option(
        DEFAULT_APP_NAME, "--app-name", "-a", help="Target application name."
    ),
    retrain: bool = typer.Option(False, "--retrain", "-r", help="Retrain LSTM before deploying."),
    horizon: str = typer.Option(
        DEFAULT_HORIZON, "--horizon", help="Prediction horizon target column."
    ),
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
    epochs: int = typer.Option(DEFAULT_EPOCHS, "--epochs", help="Training epochs."),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, "--lookback", help="Lookback window steps."),
    patience: int = typer.Option(20, "--patience", help="Early stopping patience."),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip Docker image rebuild."),
    delete_hpa: bool | None = typer.Option(
        None, "--delete-hpa/--keep-hpa", help="Delete or keep existing HPA."
    ),
    no_watch: bool = typer.Option(
        False, "--no-watch", help="Don't tail operator logs after deploy."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned steps without executing."),
) -> None:
    """
    [bold]Deploy the PPA operator[/] — retrain → convert → push models → apply CRs.

    Replaces [dim]scripts/ppa_redeploy.sh[/dim] with Rich UI and direct Python integrations.

    Examples:
        ppa deploy                          # Deploy with existing models
        ppa deploy --retrain                # Retrain and deploy
        ppa deploy --retrain --horizon 3min # Retrain 3-minute model
        ppa deploy --dry-run                # Show planned steps
    """
    if ctx.invoked_subcommand is not None:
        return

    cmd = DeployCommand()
    cmd.run(
        app_name=app_name,
        retrain=retrain,
        horizon=horizon,
        csv=csv,
        epochs=epochs,
        lookback=lookback,
        patience=patience,
        skip_build=skip_build,
        delete_hpa=delete_hpa,
        no_watch=no_watch,
        dry_run=dry_run,
    )
