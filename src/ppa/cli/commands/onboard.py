"""ppa onboard — Generate & apply PredictiveAutoscaler CRs (replaces onboard_app.sh)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from ppa.config import DEFAULT_NAMESPACE, DEPLOY_DIR, PROJECT_DIR
from ppa.cli.utils import console, error, info, kubectl, success

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def onboard(
    ctx: typer.Context,
    app_name: str = typer.Option(
        ...,
        "--app-name",
        "-a",
        help="Logical application name (used for /models/{app}).",
    ),
    target: str = typer.Option(..., "--target", "-t", help="Target Kubernetes Deployment name."),
    namespace: str = typer.Option(DEFAULT_NAMESPACE, "--namespace", "-n", help="Target namespace."),
    min_replicas: int = typer.Option(1, "--min-replicas", help="Minimum replicas."),
    max_replicas: int = typer.Option(10, "--max-replicas", help="Maximum replicas."),
    rps_capacity: int = typer.Option(20, "--rps-capacity", help="RPS capacity per pod."),
    safety_factor: float = typer.Option(1.15, "--safety-factor", help="Safety multiplier buffer."),
    scale_up_rate: float = typer.Option(2.0, "--scale-up", help="Max scale-up rate."),
    scale_down_rate: float = typer.Option(1.0, "--scale-down", help="Max scale-down rate."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate manifests without applying."),
) -> None:
    """
    [bold]Onboard a new application[/] with PredictiveAutoscaler CRs.

    Generates 3 CRs for 3m (observer), 5m (observer), and 10m (active scaler)
    horizons, then applies them to the cluster.
    """
    if ctx.invoked_subcommand is not None:
        return

    template_path = DEPLOY_DIR / "templates" / "predictiveautoscaler.yaml.tpl"
    if not template_path.exists():
        error(f"Template not found: {template_path}")
        raise typer.Exit(1)

    template = template_path.read_text()

    output_dir = DEPLOY_DIR / "generated-manifests" / app_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Config banner ─────────────────────────────────────────────────
    config_table = Table(show_header=False, border_style="bright_magenta", padding=(0, 2))
    config_table.add_column("Key", style="info")
    config_table.add_column("Value")
    for k, v in {
        "App Name": app_name,
        "Target Deployment": target,
        "Namespace": namespace,
        "Replicas": f"{min_replicas} – {max_replicas}",
        "RPS/pod": str(rps_capacity),
        "Safety Factor": str(safety_factor),
    }.items():
        config_table.add_row(k, v)
    console.print()
    console.print(
        Panel(
            config_table,
            title="[bold bright_cyan]Onboard Application[/]",
            border_style="bright_cyan",
        )
    )

    # ── Generate manifests ────────────────────────────────────────────
    horizons = [
        ("rps_t3m", True, "Observer"),
        ("rps_t5m", True, "Observer"),
        ("rps_t10m", False, "Active Scaler"),
    ]

    results_table = Table(
        title="[bold]Generated Manifests[/]",
        border_style="bright_cyan",
        header_style="bold bright_magenta",
    )
    results_table.add_column("Horizon", style="bold")
    results_table.add_column("Mode", style="info")
    results_table.add_column("File", style="dim")

    env_vars = {
        "$APP_NAME": app_name,
        "$TARGET_DEPLOYMENT": target,
        "$NAMESPACE": namespace,
        "$MIN_REPLICAS": str(min_replicas),
        "$MAX_REPLICAS": str(max_replicas),
        "$RPS_CAPACITY": str(rps_capacity),
        "$SAFETY_FACTOR": str(safety_factor),
        "$SCALE_UP_RATE": str(scale_up_rate),
        "$SCALE_DOWN_RATE": str(scale_down_rate),
    }

    generated_files: list[Path] = []

    for horizon, observer, label in horizons:
        horizon_clean = horizon.replace("_", "-")
        vars_with_horizon = {
            **env_vars,
            "$HORIZON": horizon,
            "$HORIZON_CLEAN": horizon_clean,
            "$OBSERVER_MODE": "true" if observer else "false",
        }

        rendered = template
        for var, val in vars_with_horizon.items():
            rendered = rendered.replace(var, val)

        out_file = output_dir / f"ppa-{horizon}.yaml"
        out_file.write_text(rendered)
        generated_files.append(out_file)
        results_table.add_row(horizon, label, str(out_file.relative_to(PROJECT_DIR)))

    console.print()
    console.print(results_table)

    # ── Apply manifests ───────────────────────────────────────────────
    if not dry_run:
        console.print()
        info("Applying manifests...")
        for f in generated_files:
            kubectl("apply", "-f", str(f))
            success(f"Applied {f.name}")

        console.print()
        console.print(
            Panel(
                f"[success]Successfully onboarded '{app_name}'[/success]\n\n"
                f"Models should be uploaded to /models/{app_name} by running:\n"
                f"  [accent]ppa deploy --app-name {app_name} --retrain[/accent]",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        console.print()
        info("DRY RUN — manifests generated but not applied")
