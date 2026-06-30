"""Top-level Typer app — only `ppa model push` is registered."""

from __future__ import annotations

import typer

from ppa.cli.commands.model import app as model_app

app = typer.Typer(
    name="ppa",
    help="PPA CLI — exposes `ppa model push` only. Use kubectl/docker for the rest.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(
    model_app,
    name="model",
    help="Push trained models to the operator's PVC via a loader pod.",
)
