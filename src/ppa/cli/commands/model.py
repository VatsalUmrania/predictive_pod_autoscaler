"""`ppa model` — only `push` is registered."""

from __future__ import annotations

import typer

app = typer.Typer(rich_markup_mode="rich")


@app.command("push")
def model_push(
    app_name: str = typer.Option("test-app", "--app-name", "-a"),
    namespace: str = typer.Option("default", "--namespace", "-n"),
    horizon: str = typer.Option(
        "rps_t3m,rps_t5m,rps_t10m",
        "--horizon",
        "-h",
        help="Comma-separated horizons",
    ),
    pvc_name: str = typer.Option("ppa-models", "--pvc"),
    image: str = typer.Option("python:3.11-slim", "--image"),
    data: str = typer.Option(None, "--data", "-d", help="Path to training CSV"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Push trained models to the operator's `ppa-models` PVC via a loader pod."""
    from ppa.cli.commands.push import push_models

    horizons = [h.strip() for h in horizon.split(",")]
    result = push_models(
        app_name=app_name,
        horizons=horizons,
        namespace=namespace,
        pvc_name=pvc_name,
        image=image,
        data_csv=data,
        dry_run=dry_run,
    )
    if not result.success:
        raise typer.Exit(1)
