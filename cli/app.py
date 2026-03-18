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
    version: Optional[bool] = typer.Option(
        None, "--version", "-v",
        help="Show PPA CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
):
    """Main entry point for PPA CLI."""
    if debug:
        app.pretty_exceptions_enable = True
    
    if ctx.invoked_subcommand is None:
        console.print(get_banner())
        console.print(f"\n[italic]Run [bold]ppa help[/bold] or [bold]ppa --help[/bold] for instructions.[/italic]")


@app.command("help", hidden=True)
def help_cmd(ctx: typer.Context):
    """Show global help with examples."""
    console.print(get_banner())
    console.print("\n[bold]Usage:[/] ppa [OPTIONS] COMMAND [ARGS]...")
    console.print("\n[bold]Examples:[/]")
    console.print("  ppa startup --follow      # Bootstrap and monitor cluster")
    console.print("  ppa status                # Check infrastructure health")
    console.print("  ppa cleanup               # Stop all background services")
    
    from typer.main import get_command
    click_command = get_command(app)
    with typer.Context(click_command) as ctx:
        console.print(ctx.get_help())


@app.command("cleanup")
def cleanup_cmd():
    """Stop all background PPA services and remove session file."""
    from cli.utils import cleanup_session
    cleanup_session()


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
from cli.commands.follow import app as follow_app      # noqa: E402

app.add_typer(startup_app, name="startup", help="[bold]Bootstrap[/] the cluster infrastructure (replaces ppa_startup.sh).")
app.add_typer(deploy_app,  name="deploy",  help="[bold]Deploy[/] operator: train → convert → push → apply (replaces ppa_redeploy.sh).")
app.add_typer(onboard_app, name="onboard", help="[bold]Onboard[/] a new application with PredictiveAutoscaler CRs.")
app.add_typer(monitor_app, name="monitor", help="[bold]Live dashboard[/] — HPA vs PPA comparison with prediction validation.")
app.add_typer(data_app,    name="data",    help="[bold]Data[/] collection — export, validate, and inspect training data.")
app.add_typer(model_app,   name="model",   help="[bold]ML model[/] — train, evaluate, convert, and run full pipeline.")
app.add_typer(status_app,  name="status",  help="[bold]Cluster status[/] — health check for all PPA components.")
app.add_typer(toolbox_app, name="toolbox", help="[bold]Toolbox[/] — utility and diagnostic commands for debugging.")
app.add_typer(follow_app,  name="follow",  help="[bold]Follow[/] live logs and health (auto-cleanup on exit).")
