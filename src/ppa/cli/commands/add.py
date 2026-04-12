"""ppa add — Register an app for autoscaling.

Replaces `ppa onboard`. Generates PredictiveAutoscaler manifests
and applies them to the cluster. Business logic reused from onboard.py.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

import typer

from ppa.cli.utils import (
    console,
    error_block,
    info,
    kubectl,
    next_step,
    success,
)
from ppa.config import DEFAULT_NAMESPACE, DEPLOY_DIR, PROJECT_DIR


def add_cmd(
    app_name: str = typer.Option(
        ..., "--app-name", "-a", help="Logical app name (used for /models/{app})."
    ),
    target: str = typer.Option(
        ..., "--target", "-t", help="Target Kubernetes Deployment name."
    ),
    namespace: str = typer.Option(
        DEFAULT_NAMESPACE, "--namespace", "-n", help="Target namespace."
    ),
    min_replicas: int = typer.Option(1, "--min-replicas", help="Minimum replica count."),
    max_replicas: int = typer.Option(10, "--max-replicas", help="Maximum replica count."),
    rps_capacity: int = typer.Option(20, "--rps-capacity", help="RPS capacity per pod."),
    safety_factor: float = typer.Option(1.15, "--safety-factor", help="Safety multiplier buffer."),
    scale_up: float = typer.Option(2.0, "--scale-up", help="Max scale-up rate."),
    scale_down: float = typer.Option(1.0, "--scale-down", help="Max scale-down rate."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Generate manifests without applying."),
) -> None:
    """Register an app for autoscaling.

    Generates PredictiveAutoscaler CRs for 3m, 5m, and 10m horizons,
    then applies them to the cluster.

    \b
    EXAMPLES
      ppa add --app-name payments --target payments-deploy
      ppa add --app-name api --target api-deploy -n prod
      ppa add --app-name api --target api-deploy --dry-run

    \b
    REQUIRES
      • ppa init must have been run
      • Target deployment must exist in cluster
    """
    template_path = DEPLOY_DIR / "templates" / "predictiveautoscaler.yaml.tpl"
    if not template_path.exists():
        error_block(
            "Template not found",
            cause=f"Expected at {template_path}",
            fix="ppa init",
        )
        raise typer.Exit(1)

    template = template_path.read_text()
    output_dir = DEPLOY_DIR / "generated-manifests" / app_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Show registration info
    console.print()
    console.print(f"  Registering [bold]{app_name}[/]...")
    console.print()

    # Generate manifests
    horizons = [
        ("rps_t3m", True, "Observer"),
        ("rps_t5m", True, "Observer"),
        ("rps_t10m", False, "Active Scaler"),
    ]

    env_vars = {
        "APP_NAME": app_name,
        "TARGET_DEPLOYMENT": target,
        "NAMESPACE": namespace,
        "MIN_REPLICAS": str(min_replicas),
        "MAX_REPLICAS": str(max_replicas),
        "RPS_CAPACITY": str(rps_capacity),
        "SAFETY_FACTOR": str(safety_factor),
        "SCALE_UP_RATE": str(scale_up),
        "SCALE_DOWN_RATE": str(scale_down),
    }

    generated_files: list[Path] = []

    for horizon, observer, _label in horizons:
        horizon_clean = horizon.replace("_", "-")
        vars_with_horizon = {
            **env_vars,
            "HORIZON": horizon,
            "HORIZON_CLEAN": horizon_clean,
            "OBSERVER_MODE": "true" if observer else "false",
        }

        rendered = Template(template).substitute(**vars_with_horizon)
        out_file = output_dir / f"ppa-{horizon}.yaml"
        out_file.write_text(rendered)
        generated_files.append(out_file)

    if dry_run:
        console.print("  Dry run — manifests generated but not applied.")
        console.print()
        for f in generated_files:
            info(f"Generated: {f.relative_to(PROJECT_DIR)}")
        console.print()
        console.print(f"  To apply:  [bold]ppa add --app-name {app_name} --target {target}[/]")
        raise typer.Exit()

    # Self-healing: Ensure CRD exists before applying CRs
    _ensure_crd()

    # Apply manifests
    info("Applying manifests to cluster...")
    for f in generated_files:
        kubectl("apply", "-f", str(f))

    # Success output
    console.print()
    success(f"{app_name} registered")
    console.print()
    console.print(f"     Target         {target}")
    console.print(f"     Namespace      {namespace}")
    console.print(f"     Replicas       {min_replicas} – {max_replicas}")
    console.print(f"     RPS capacity   {rps_capacity} per pod")
    console.print(f"     Safety factor  {safety_factor}×")
    console.print(f"     Scale up       {scale_up}×  max")
    console.print(f"     Scale down     {scale_down}×  max")
    console.print()
    console.print("  Manifests applied to cluster.")

    next_step(f"ppa train --app {app_name}", "train the LSTM model")


def _ensure_crd() -> None:
    """Ensure the PredictiveAutoscaler CRD exists in the cluster."""
    from ppa.cli.utils import run_cmd_silent

    # Try to get the CRD
    result = run_cmd_silent(
        ["kubectl", "get", "crd", "predictiveautoscalers.ppa.example.com"],
        check=False
    )

    # If missing, install it
    if result.returncode != 0:
        info("PredictiveAutoscaler CRD not found. Installing...")
        crd_path = DEPLOY_DIR / "crd.yaml"

        if not crd_path.exists():
            error_block(
                "CRD manifest not found",
                cause=f"Expected at {crd_path}",
                fix="Re-install PPA or check deploy/ directory.",
            )
            raise typer.Exit(1)

        kubectl("apply", "-f", str(crd_path))
        success("CRD installed successfully")
