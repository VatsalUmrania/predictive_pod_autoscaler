"""ppa deploy — Train → Convert → Deploy operator (replaces ppa_redeploy.sh).

Translates the 9-step redeploy script into Python with Rich UI.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from cli.config import (
    ARTIFACTS_DIR,
    CHAMPION_DIR,
    DEFAULT_APP_NAME,
    DEFAULT_CSV,
    DEFAULT_EPOCHS,
    DEFAULT_HORIZON,
    DEFAULT_LOOKBACK,
    DEFAULT_NAMESPACE,
    DEPLOY_DIR,
    PROJECT_DIR,
)
from cli.utils import (
    console,
    error,
    get_minikube_docker_env,
    heading,
    info,
    kubectl,
    run_cmd,
    run_cmd_silent,
    step_heading,
    success,
    warn,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def deploy(
    ctx: typer.Context,
    app_name: str = typer.Option(DEFAULT_APP_NAME, "--app-name", "-a", help="Target application name."),
    retrain: bool = typer.Option(False, "--retrain", "-r", help="Retrain LSTM before deploying."),
    horizon: str = typer.Option(DEFAULT_HORIZON, "--horizon", help="Prediction horizon target column."),
    csv: str = typer.Option(DEFAULT_CSV, "--csv", help="Path to training CSV."),
    epochs: int = typer.Option(DEFAULT_EPOCHS, "--epochs", help="Training epochs."),
    lookback: int = typer.Option(DEFAULT_LOOKBACK, "--lookback", help="Lookback window steps."),
    patience: int = typer.Option(20, "--patience", help="Early stopping patience."),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip Docker image rebuild."),
    delete_hpa: bool | None = typer.Option(None, "--delete-hpa/--keep-hpa", help="Delete or keep existing HPA."),
    no_watch: bool = typer.Option(False, "--no-watch", help="Don't tail operator logs after deploy."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show planned steps without executing."),
) -> None:
    """
    [bold]Deploy the PPA operator[/] — retrain → convert → push models → apply CRs.

    Replaces [dim]scripts/ppa_redeploy.sh[/dim] with Rich UI and direct Python integrations.
    """
    if ctx.invoked_subcommand is not None:
        return

    artifacts_dir = str(ARTIFACTS_DIR)
    champion_dir = str(CHAMPION_DIR / app_name / horizon)
    venv_path = str(PROJECT_DIR / "venv")

    # ── Banner ──────────────────────────────────────────────────────────
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
        console.print(Panel(table, title="[bold bright_cyan]PPA Deploy[/]", border_style="bright_cyan"))

    if dry_run:
        heading("DRY RUN — Planned steps")
        steps = ["Retrain LSTM", "Convert → TFLite", "Promote artifacts"] if retrain else []
        steps += ["Handle HPA", "Scale down operator", "Build Docker image", "Push models to PVC", "Deploy operator", "Apply CR"]
        for i, s in enumerate(steps, 1):
            info(f"Step {i}: {s}")
        raise typer.Exit()

    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    
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

        # ── Step 1–3: Retrain + Convert + Promote ─────────────────────────
        if retrain:
            current += 1
            step_heading(current, total_steps, f"Retraining LSTM ({horizon})")
            progress.update(task, description=f"[bold]Step {current}:[/bold] Retraining LSTM")

            if not os.path.exists(csv):
                error(f"Training CSV not found: {csv}")
                raise typer.Exit(1)

            # Direct Python import for training
            sys.path.insert(0, str(PROJECT_DIR))
            try:
                from model.train import train_model
                result = train_model(
                    csv_path=csv,
                    lookback=lookback,
                    epochs=epochs,
                    target_col=horizon,
                    output_dir=artifacts_dir,
                    early_stopping_patience=patience,
                )
                if result is None:
                    error("Training failed")
                    raise typer.Exit(1)
                success(f"Training complete — Val MAE: {result['metrics']['val_mae']:.4f}")
            except ImportError:
                warn("Cannot import model.train — falling back to subprocess")
                run_cmd(
                    [
                        "python", str(PROJECT_DIR / "model" / "train.py"),
                        "--csv", csv, "--lookback", str(lookback),
                        "--epochs", str(epochs), "--target", horizon,
                        "--output-dir", artifacts_dir, "--patience", str(patience),
                    ],
                    title="Training LSTM",
                )
            
            progress.advance(task)

            # Convert
            current += 1
            step_heading(current, total_steps, "Converting Keras → TFLite")
            progress.update(task, description=f"[bold]Step {current}:[/bold] Converting model")
            keras_model = os.path.join(artifacts_dir, f"ppa_model_{horizon}.keras")
            tflite_out = os.path.join(artifacts_dir, "ppa_model.tflite")

            try:
                from model.convert import convert_model
                conv = convert_model(model_path=keras_model, output_path=tflite_out)
                if conv:
                    success(f"Converted → {tflite_out} ({conv['size_kb']:.1f} KB)")
                else:
                    error("Conversion failed")
                    raise typer.Exit(1)
            except ImportError:
                run_cmd(
                    ["python", str(PROJECT_DIR / "model" / "convert.py"), "--model", keras_model, "--output", tflite_out],
                    title="Converting to TFLite",
                )
            
            progress.advance(task)

            # Promote
            current += 1
            step_heading(current, total_steps, f"Promoting artifacts")
            progress.update(task, description=f"[bold]Step {current}:[/bold] Promoting artifacts")
        os.makedirs(champion_dir, exist_ok=True)
        import shutil
        shutil.copy2(tflite_out, os.path.join(champion_dir, "ppa_model.tflite"))
        for src_suffix, dst_name in [
            (f"scaler_{horizon}.pkl", "scaler.pkl"),
            (f"target_scaler_{horizon}.pkl", "target_scaler.pkl"),
        ]:
            src = os.path.join(artifacts_dir, src_suffix)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(champion_dir, dst_name))
        success("Artifacts promoted to champions")
        progress.advance(task)

    # ── Verify champion dir ───────────────────────────────────────────
    if not os.path.isdir(champion_dir):
        error(f"Champion dir not found: {champion_dir}\nRun with --retrain or train manually first.")
        raise typer.Exit(1)

    # ── Step: Handle HPA ──────────────────────────────────────────────
    current += 1
    step_heading(current, total_steps, "Checking HPA")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Checking HPA")

    hpa_result = kubectl("get", "hpa", app_name, namespace=DEFAULT_NAMESPACE, check=False)
    if hpa_result.returncode == 0:
        warn(f"HPA '{app_name}' is active — may conflict with PPA")
        if delete_hpa is None:
            delete_hpa = Confirm.ask("  Delete HPA now?", default=False, console=console)
        if delete_hpa:
            kubectl("delete", "hpa", app_name, namespace=DEFAULT_NAMESPACE)
            success("HPA deleted")
        else:
            warn("Keeping HPA — PPA and HPA will both run")
    else:
        success(f"No HPA found for '{app_name}'")
    progress.advance(task)

    # ── Step: Scale down operator ─────────────────────────────────────
    current += 1
    step_heading(current, total_steps, "Scaling down existing operator")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Scaling down operator")

    op_result = kubectl("get", "deployment", "ppa-operator", namespace=DEFAULT_NAMESPACE, check=False)
    if op_result.returncode == 0:
        kubectl("scale", "deployment", "ppa-operator", "--replicas=0", namespace=DEFAULT_NAMESPACE)
        success("Operator scaled to 0")
    else:
        success("No existing operator deployment")
    progress.advance(task)

    # ── Step: Build Docker image ──────────────────────────────────────
    current += 1
    if not skip_build:
        step_heading(current, total_steps, "Building ppa-operator:latest")
        progress.update(task, description=f"[bold]Step {current}:[/bold] Building operator image")
        docker_env = {**os.environ, **get_minikube_docker_env()}
        run_cmd(
            [
                "docker", "build", "-t", "ppa-operator:latest",
                "-f", str(PROJECT_DIR / "operator" / "Dockerfile"),
                str(PROJECT_DIR),
            ],
            title="Building ppa-operator Docker image",
            env=docker_env,
        )
        success("Image built: ppa-operator:latest")
    else:
        step_heading(current, total_steps, "Skipping image build")
        progress.update(task, description=f"[bold]Step {current}:[/bold] Skipping image build")
    progress.advance(task)

    # ── Step: Apply CRD + RBAC ────────────────────────────────────────
    current += 1
    step_heading(current, total_steps, "Applying CRD + RBAC + Deployment")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Deploying operator")
    kubectl("apply", "-f", str(DEPLOY_DIR / "crd.yaml"))
    kubectl("apply", "-f", str(DEPLOY_DIR / "rbac.yaml"))
    kubectl("apply", "-f", str(DEPLOY_DIR / "operator-deployment.yaml"))

    info("Waiting for operator rollout...")
    run_cmd(
        ["kubectl", "rollout", "status", "deployment/ppa-operator", f"--namespace={DEFAULT_NAMESPACE}", "--timeout=120s"],
        title="Operator rollout",
    )
    success("Operator deployment rolled out")
    progress.advance(task)

    # ── Step: Apply CR ────────────────────────────────────────────────
    current += 1
    step_heading(current, total_steps, "Applying PredictiveAutoscaler CR")
    progress.update(task, description=f"[bold]Step {current}:[/bold] Applying CR")
    kubectl("apply", "-f", str(DEPLOY_DIR / "predictiveautoscaler.yaml"))
    success("CR applied")
    progress.advance(task)

    # ── Summary ───────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        "[success]Deployment Complete ✓[/success]",
        border_style="green",
        padding=(1, 4),
    ))

    # Show CRs
    cr_result = kubectl("get", "ppa", namespace=DEFAULT_NAMESPACE, check=False)
    if cr_result.returncode == 0 and cr_result.stdout.strip():
        console.print(cr_result.stdout)

    warmup_min = lookback // 2
    warn(f"Warmup: ~{warmup_min} minutes ({lookback} × 30s steps)")

    # ── Tail logs ─────────────────────────────────────────────────────
    if not no_watch:
        console.print()
        info("Tailing operator logs — Ctrl+C to exit")
        time.sleep(3)
        try:
            # Stream logs with cross-platform filtering (no grep needed)
            import re
            import subprocess as _sp

            pattern = re.compile(r"Predicted|Scaling|Patched|Warming|ERROR|WARN|champion|model", re.IGNORECASE)
            proc = _sp.Popen(
                ["kubectl", "logs", "-l", f"app=ppa-operator", "-n", DEFAULT_NAMESPACE, "-f", "--tail=50"],
                stdout=_sp.PIPE,
                stderr=_sp.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            for line in proc.stdout or []:
                if pattern.search(line):
                    console.print(line.rstrip())

            proc.wait()
        except KeyboardInterrupt:
            success("Log tailing stopped")
