"""PPA CLI — Root Typer application with subcommand registration."""

from typing import Optional

import typer
from rich.padding import Padding

from cli import __version__
from cli.config import get_banner
from cli.utils import console


# ── Root app ─────────────────────────────────────────────────────────────────
app = typer.Typer(
    name="ppa",
    help="[bold bright_cyan]Predictive Pod Autoscaler[/] — ML-driven Kubernetes scaling CLI.",
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_enable=True,
    add_completion=True,
)


# ── Callbacks ────────────────────────────────────────────────────────────────

def _version_callback(value: bool) -> None:
    if value:
        console.print(get_banner())
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(
        None, "--version", "-v",
        help="Show PPA CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """
    [bold bright_cyan]Predictive Pod Autoscaler[/] — ML-driven Kubernetes scaling CLI.

    Replaces shell scripts with a unified, beautifully animated interface
    powered by [bold]Typer[/] + [bold]Rich[/].
    """
    if ctx.invoked_subcommand is None:
        console.print(get_banner())
        console.print(Padding("[dim]Run [bold]ppa --help[/bold] for available commands.[/dim]", (0, 2)))


# ── Subcommand groups ────────────────────────────────────────────────────────
# Each command module creates its own Typer app; we import and register them here.

from cli.commands.startup import app as startup_app    # noqa: E402
from cli.commands.deploy import app as deploy_app      # noqa: E402
from cli.commands.onboard import app as onboard_app    # noqa: E402
from cli.commands.monitor import app as monitor_app    # noqa: E402
from cli.commands.data import app as data_app          # noqa: E402
from cli.commands.model import app as model_app        # noqa: E402
from cli.commands.status import app as status_app      # noqa: E402
from cli.commands.toolbox import app as toolbox_app    # noqa: E402

app.add_typer(startup_app, name="startup", help="[bold]Bootstrap[/] the cluster infrastructure (replaces ppa_startup.sh).")
app.add_typer(deploy_app,  name="deploy",  help="[bold]Deploy[/] operator: train → convert → push → apply (replaces ppa_redeploy.sh).")
app.add_typer(onboard_app, name="onboard", help="[bold]Onboard[/] a new application with PredictiveAutoscaler CRs.")
app.add_typer(monitor_app, name="monitor", help="[bold]Live dashboard[/] — HPA vs PPA comparison with prediction validation.")
app.add_typer(data_app,    name="data",    help="[bold]Data[/] collection — export, validate, and inspect training data.")
app.add_typer(model_app,   name="model",   help="[bold]ML model[/] — train, evaluate, convert, and run full pipeline.")
app.add_typer(status_app,  name="status",  help="[bold]Cluster status[/] — health check for all PPA components.")
app.add_typer(toolbox_app, name="toolbox", help="[bold]Toolbox[/] — utility and diagnostic commands for debugging.")
