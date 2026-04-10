"""ppa model push — Push trained models to PVC."""

from __future__ import annotations

import logging
import os
import signal
import sys
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import typer

from ppa.cli.utilities.kubernetes import (
    cp,
    create_loader_pod,
    delete_pod,
    ensure_exists,
    exec_cmd,
    mkdir,
    unique_pod_name,
    validate_cluster,
    wait_for_ready,
)
from ppa.cli.utils import (
    console,
    error,
    get_minikube_docker_env,
    heading,
    info,
    success,
    warn,
)
from ppa.config import CHAMPION_DIR, PROJECT_DIR, TRAINING_DATA_DIR

app = typer.Typer(rich_markup_mode="rich")

logger = logging.getLogger(__name__)

PVC_NAME = os.getenv("PPA_MODELS_PVC", "ppa-models")
IMAGE = os.getenv("PPA_LOADER_IMAGE", "python:3.11-slim")

_active_pod: tuple[str, str] = ("", "")


def _cleanup_handler(signum, frame):
    """Cleanup pod on SIGINT/SIGTERM."""
    name, namespace = _active_pod
    if name:
        info(f"Caught signal {signum}, cleaning up...")
        delete_pod(name, namespace)
    sys.exit(1)


signal.signal(signal.SIGINT, _cleanup_handler)
signal.signal(signal.SIGTERM, _cleanup_handler)


@dataclass
class PushResult:
    success: bool = False
    horizons_pushed: list[str] = field(default_factory=list)
    horizons_skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _resolve_source_artifacts(
    app_name: str, horizon: str
) -> tuple[Path | None, Path | None, str | None]:
    """Resolve model and metadata artifacts from champion or pipeline output directories."""
    source_roots = [
        (
            CHAMPION_DIR / app_name / horizon,
            "champion",
            [
                CHAMPION_DIR / app_name / horizon / "ppa_model_metadata.json",
                CHAMPION_DIR / app_name / horizon / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "model" / "artifacts" / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "model" / "artifacts" / "ppa_model_metadata.json",
                PROJECT_DIR / "data" / "artifacts" / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "data" / "artifacts" / "ppa_model_metadata.json",
            ],
        ),
        (
            PROJECT_DIR / "model" / "artifacts",
            "model/artifacts",
            [
                PROJECT_DIR / "model" / "artifacts" / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "model" / "artifacts" / "ppa_model_metadata.json",
                PROJECT_DIR / "data" / "artifacts" / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "data" / "artifacts" / "ppa_model_metadata.json",
            ],
        ),
        (
            PROJECT_DIR / "data" / "artifacts",
            "data/artifacts",
            [
                PROJECT_DIR / "data" / "artifacts" / f"ppa_model_{horizon}_metadata.json",
                PROJECT_DIR / "data" / "artifacts" / "ppa_model_metadata.json",
            ],
        ),
    ]

    for root_dir, source_label, metadata_candidates in source_roots:
        model_candidates = [
            root_dir / "ppa_model.tflite",
            root_dir / f"ppa_model_{horizon}.tflite",
        ]

        model_path = next((candidate for candidate in model_candidates if candidate.exists()), None)
        if model_path is None:
            continue

        # FIX (PR#17): Validate model file format before pushing
        if not _validate_tflite_model(model_path):
            warn(f"Skipping {horizon}: model file invalid TFLite format")
            continue

        metadata_path = next(
            (candidate for candidate in metadata_candidates if candidate.exists()), None
        )
        return model_path, metadata_path, source_label

    return None, None, None


def _validate_tflite_model(model_path: Path) -> bool:
    """
    Validate that a file is a valid TFLite model.

    Returns True if valid, False otherwise.
    """
    try:
        with open(model_path, "rb") as f:
            # TFLite files have "TFL3" at offset 4
            f.seek(4)
            magic = f.read(4)
            return magic == b"TFL3"
    except Exception as e:
        logger.warning(f"Failed to validate model {model_path}: {e}")
        return False


def push_models(
    app_name: str,
    horizons: list[str],
    *,
    namespace: str = "default",
    pvc_name: str = PVC_NAME,
    image: str = IMAGE,
    data_csv: str | None = None,
    dry_run: bool = False,
) -> PushResult:
    """Push trained models to PVC via loader pod."""

    global _active_pod
    result = PushResult()

    if not validate_cluster():
        error("kubectl not configured or cluster not accessible")
        result.errors.append("Cluster validation failed")
        return result

    if data_csv is None:
        data_csv = str(TRAINING_DATA_DIR / "training_data_v2.csv")

    heading("Pushing Models to PVC")
    info(f"App: {app_name}")
    info(f"Horizons: {', '.join(horizons)}")
    info(f"PVC: {pvc_name}")

    for h in horizons:
        model_file, _, source_label = _resolve_source_artifacts(app_name, h)
        if model_file is not None:
            result.horizons_pushed.append(h)
            if source_label:
                info(f"Using {source_label} artifacts for {h}")
        else:
            result.horizons_skipped.append(h)
            warn(f"Skipping {h}: no model")

    if not result.horizons_pushed:
        error("No models to push")
        return result

    if not Path(data_csv).exists():
        error(f"CSV not found: {data_csv}")
        result.errors.append(f"CSV not found: {data_csv}")
        return result

    info(f"Will push: {', '.join(result.horizons_pushed)}")

    if dry_run:
        info("[dry-run] Skipping push")
        result.success = True
        return result

    loader_image = "ppa-loader:latest"
    loader_dockerfile = PROJECT_DIR / "src" / "ppa" / "loader" / "Dockerfile"
    if loader_dockerfile.exists():
        info("Building loader image...")
        docker_env = {**os.environ, **get_minikube_docker_env()}
        docker_env["DOCKER_BUILDKIT"] = "0"
        import subprocess as _sub

        _sub.run(
            [
                "docker",
                "build",
                "-f",
                str(loader_dockerfile),
                "-t",
                loader_image,
                str(PROJECT_DIR),
            ],
            env=docker_env,
            check=True,
        )
        info("Loader image built (ready in minikube docker)")
        success("Image ready in minikube")
    else:
        loader_image = "python:3.11-slim"

    ensure_exists(pvc_name, namespace)
    success("PVC ready")

    pod_name = unique_pod_name()
    pod_path = f"{namespace}/{pod_name}"
    _active_pod = (pod_name, namespace)

    info(f"Creating loader pod: {pod_name}")
    create_loader_pod(pod_name, loader_image, pvc_name, namespace)

    if not wait_for_ready(pod_name, namespace):
        error("Loader pod failed to start")
        delete_pod(pod_name, namespace)
        _active_pod = ("", "")
        result.errors.append("Pod failed to start")
        return result

    success("Loader pod ready")

    try:
        regen_script = PROJECT_DIR / "src" / "ppa" / "runtime" / "regenerate_scalers.py"
        cp(str(regen_script), f"{namespace}/{pod_name}:/tmp/regenerate.py")
        info("Regeneration script copied")

        remote_dirs = [f"/models/{h}" for h in result.horizons_pushed]
        mkdir(pod_path, *remote_dirs)
        info("Directories created")

        for h in result.horizons_pushed:
            model_file, metadata_file, _ = _resolve_source_artifacts(app_name, h)
            if model_file is None:
                warn(f"Skipping {h}: no source artifact found during push")
                continue

            local_dir = CHAMPION_DIR / app_name / h
            local_dir.mkdir(parents=True, exist_ok=True)

            if model_file != local_dir / "ppa_model.tflite":
                shutil.copy2(model_file, local_dir / "ppa_model.tflite")

            cp(
                str(local_dir / "ppa_model.tflite"),
                f"{namespace}/{pod_name}:/models/{h}/ppa_model.tflite",
            )
            info(f"Copied {h}/ppa_model.tflite")

            if metadata_file is not None:
                canonical_metadata = local_dir / "ppa_model_metadata.json"
                if metadata_file != canonical_metadata:
                    shutil.copy2(metadata_file, canonical_metadata)

                cp(
                    str(canonical_metadata),
                    f"{namespace}/{pod_name}:/models/{h}/ppa_model_metadata.json",
                )
                info(f"Copied {h}/ppa_model_metadata.json")
            else:
                warn(f"Skipping {h}: no metadata found")

        cp(data_csv, f"{namespace}/{pod_name}:/tmp/training_data.csv")
        info("Training data copied")

        horizon_arg = ",".join(result.horizons_pushed)
        info(f"Regenerating scalers: {horizon_arg}")

        exec_result = exec_cmd(
            pod_path,
            "python3",
            "/tmp/regenerate.py",
            app_name,
            horizon_arg,
            "/tmp/training_data.csv",
        )

        if exec_result.returncode != 0:
            error("Scaler regeneration failed")
            if exec_result.stderr:
                error(f"Script output: {exec_result.stderr}")
            result.errors.append(exec_result.stderr or "Regeneration failed")
            return result

        success("Scalers regenerated")
        console.print(exec_result.stdout)

        for h in result.horizons_pushed:
            local_dir = CHAMPION_DIR / app_name / h
            local_dir.mkdir(parents=True, exist_ok=True)

            for fname in ["scaler.pkl", "target_scaler.pkl"]:
                cp(
                    f"{namespace}/{pod_name}:/models/{h}/{fname}",
                    str(local_dir / fname),
                )

        success("Scalers synced to host")

        list_result = exec_cmd(
            pod_path, "find", "/models", "-name", "*.tflite", "-o", "-name", "*.pkl"
        )
        console.print("\n[bold]PVC Contents:[/bold]")
        console.print(list_result.stdout)

        result.success = True

    finally:
        info("Cleaning up loader pod...")
        delete_pod(pod_name, namespace)
        _active_pod = ("", "")

    console.print()
    success(f"Pushed {len(result.horizons_pushed)} horizons: {', '.join(result.horizons_pushed)}")

    return result


@app.command("push")
def push_command(
    app_name: str = typer.Option("test-app", "--app-name", "-a"),
    namespace: str = typer.Option("default", "--namespace", "-n"),
    horizon: str = typer.Option(
        "rps_t3m,rps_t5m,rps_t10m", "--horizon", "-h", help="Comma-separated horizons"
    ),
    pvc_name: str = typer.Option(PVC_NAME, "--pvc"),
    image: str = typer.Option(IMAGE, "--image"),
    data: str | None = typer.Option(None, "--data", "-d", help="Path to training CSV"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """[bold]Push[/] trained models to PVC via loader pod."""
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
