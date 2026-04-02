"""PPA CLI — Root Typer application with subcommand registration."""

import typer

from ppa.cli.startup import print_startup_screen
from ppa.cli.utils import console
from ppa.config import get_banner

# ── Root app ─────────────────────────────────────────────────────────────────
app = typer.Typer(
    name="ppa",
    help="[bold blue]Predictive Pod Autoscaler (PPA)[/] • Intelligent Kubernetes Scaling.",
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # Deliverable 6: No stack traces by default
    add_completion=True,
)


# ── Callbacks ────────────────────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        console.print(get_banner())
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug mode and stack traces."),
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Show PPA CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
):
    """Main entry point for PPA CLI."""
    if debug:
        app.pretty_exceptions_enable = True

    if ctx.invoked_subcommand is None:
        print_startup_screen()


@app.command("help", hidden=True)
def help_cmd(ctx: typer.Context):
    """Show global help with getting started guide."""
    print_startup_screen()
    console.print()
    console.print("[bold cyan]Global Options:[/]")
    console.print(ctx.get_help())


@app.command("cleanup")
def cleanup_cmd():
    """Stop all background PPA services and remove session file."""
    from ppa.cli.utils import cleanup_session

    cleanup_session()


@app.command("guide", hidden=False)
def guide_cmd(
    command: str = typer.Argument(None, help="Command name (e.g., startup, deploy)")
):
    """Show detailed guide for a command.

    Examples:
        ppa guide startup
        ppa guide deploy
        ppa guide monitor
    """
    from ppa.cli.help import print_help_for_command

    if not command:
        console.print("[error]Usage: ppa guide <command>[/]")
        console.print(
            "[dim]Examples: ppa guide startup | ppa guide deploy | ppa guide monitor[/]"
        )
        return

    print_help_for_command(command)


# ── Subcommand groups ────────────────────────────────────────────────────────
# Each command module creates its own Typer app; we import and register them here.

from ppa.cli.commands.data import app as data_app  # noqa: E402
from ppa.cli.commands.deploy import app as deploy_app  # noqa: E402
from ppa.cli.commands.follow import app as follow_app  # noqa: E402
from ppa.cli.commands.model import app as model_app  # noqa: E402
from ppa.cli.commands.monitor import app as monitor_app  # noqa: E402
from ppa.cli.commands.onboard import app as onboard_app  # noqa: E402
from ppa.cli.commands.operator import app as operator_app  # noqa: E402
from ppa.cli.commands.startup import app as startup_app  # noqa: E402
from ppa.cli.commands.status import app as status_app  # noqa: E402
from ppa.cli.commands.toolbox import app as toolbox_app  # noqa: E402

app.add_typer(
    startup_app,
    name="startup",
    help="[bold]Bootstrap[/] the cluster infrastructure (replaces ppa_startup.sh).",
)
app.add_typer(
    deploy_app,
    name="deploy",
    help="[bold]Deploy[/] operator: train → convert → push → apply (replaces ppa_redeploy.sh).",
)
app.add_typer(
    onboard_app,
    name="onboard",
    help="[bold]Onboard[/] a new application with PredictiveAutoscaler CRs.",
)
app.add_typer(
    monitor_app,
    name="monitor",
    help="[bold]Live dashboard[/] — HPA vs PPA comparison with prediction validation.",
)
app.add_typer(
    data_app,
    name="data",
    help="[bold]Data[/] collection — export, validate, and inspect training data.",
)
app.add_typer(
    model_app,
    name="model",
    help="[bold]ML model[/] — train, evaluate, convert, and run full pipeline.",
)
app.add_typer(
    status_app,
    name="status",
    help="[bold]Cluster status[/] — health check for all PPA components.",
)
app.add_typer(
    toolbox_app,
    name="toolbox",
    help="[bold]Toolbox[/] — utility and diagnostic commands for debugging.",
)
app.add_typer(
    follow_app,
    name="follow",
    help="[bold]Follow[/] live logs and health (auto-cleanup on exit).",
)
app.add_typer(
    operator_app,
    name="operator",
    help="[bold]Operator[/] lifecycle — build, deploy, restart, and status.",
)
