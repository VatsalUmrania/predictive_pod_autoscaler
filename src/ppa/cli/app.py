"""PPA CLI — Root Typer application with grouped command registration.

Command tree matches ppa-cli-spec.md Section 2 exactly.
Help output is grouped using Typer rich_help_panel.
"""

from __future__ import annotations

import typer

from ppa import __version__
from ppa.cli.banner import print_banner_inline
from ppa.cli.utils import console

app = typer.Typer(
    name="ppa",
    help=f"""PPA  v{__version__}

Predictive pod autoscaler — ML-based Kubernetes scaling

[bold]QUICK START[/]
  ppa run --app myapp          [dim]# bootstrap + train + deploy in one step[/dim]
  ppa train --apply            [dim]# retrain and redeploy[/dim]
  ppa status                   [dim]# check everything is healthy[/dim]

  ppa <command> --help         [dim]for command-level help and examples[/dim]""",
    rich_markup_mode="rich",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=True,
    context_settings={"help_option_names": ["--help"]},
)


def _version_callback(value: bool) -> None:
    if value:
        print_banner_inline(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    debug: bool = typer.Option(
        False, "--debug", "-d", help="Enable debug mode and stack traces."
    ),
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help="Show CLI version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Main entry point for PPA CLI."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug

    if debug:
        app.pretty_exceptions_enable = True


# --- LIFECYCLE ---
from ppa.cli.commands.add import add_cmd  # noqa: E402
from ppa.cli.commands.apply import apply_cmd  # noqa: E402
from ppa.cli.commands.init import init_cmd  # noqa: E402
from ppa.cli.commands.run import run_cmd  # noqa: E402
from ppa.cli.commands.train import train_cmd  # noqa: E402

app.command("init", help="Bootstrap cluster infrastructure", rich_help_panel="LIFECYCLE")(init_cmd)
app.command("add", help="Register an app for autoscaling", rich_help_panel="LIFECYCLE")(add_cmd)
app.command("train", help="Train LSTM model(s): by default, all models: rps_t3m, rps_t5m, and rps_t10m", rich_help_panel="LIFECYCLE")(train_cmd)
app.command("apply", help="Deploy autoscaler with the current model", rich_help_panel="LIFECYCLE")(apply_cmd)
app.command("run", help="Full lifecycle: init → add → train → apply", rich_help_panel="LIFECYCLE")(run_cmd)

# --- OBSERVE ---
from ppa.cli.commands.logs import logs_cmd  # noqa: E402
from ppa.cli.commands.status import status_app  # noqa: E402
from ppa.cli.commands.watch import watch_cmd  # noqa: E402

app.command("watch", help="Live dashboard — HPA vs PPA with predictions", rich_help_panel="OBSERVE")(watch_cmd)
app.add_typer(status_app, name="status", help="System health — infra, operator, model", rich_help_panel="OBSERVE")
app.command("logs", help="Log output  (--follow to stream live)", rich_help_panel="OBSERVE")(logs_cmd)

# --- ML & DATA ---
from ppa.cli.commands.data import app as data_app  # noqa: E402
from ppa.cli.commands.model import app as model_app  # noqa: E402

app.add_typer(model_app, name="model", help="Evaluate, convert, and push ML models", rich_help_panel="ML & DATA")
app.add_typer(data_app, name="data", help="Training data — export, validate, inspect", rich_help_panel="ML & DATA")

# --- SYSTEM ---
from ppa.cli.commands.config_cmd import config_app  # noqa: E402
from ppa.cli.commands.operator import app as operator_app  # noqa: E402

app.add_typer(operator_app, name="operator", help="Operator image lifecycle — build, deploy, restart", rich_help_panel="SYSTEM")
app.add_typer(config_app, name="config", help="View and edit PPA configuration", rich_help_panel="SYSTEM")

@app.command("cleanup", help="Stop all PPA services and remove session", rich_help_panel="SYSTEM")
def cleanup_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Stop all background PPA services and remove session file."""
    from ppa.cli.utils import cleanup_session

    if not yes:
        confirm = typer.confirm("  This will stop all PPA services. Continue?", default=False)
        if not confirm:
            console.print("  Cancelled.  No changes made.")
            raise typer.Exit()

    cleanup_session()

# --- DEBUG ---
from ppa.cli.commands.debug import debug_app  # noqa: E402

app.add_typer(debug_app, name="debug", help="Diagnostics — HPA, traffic, test-app", rich_help_panel="DEBUG")

# --- DEPRECATED (hidden) ---
from ppa.cli.commands.deprecated import register_deprecated_commands  # noqa: E402

register_deprecated_commands(app)
