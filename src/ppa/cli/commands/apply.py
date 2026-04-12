"""ppa apply — Deploy autoscaler with the current model.

Replaces `ppa deploy`. Orchestrates a multi-stage deploy pipeline:
convert → push → build → deploy → verify. All business logic is
delegated to existing deploy modules.
"""

from __future__ import annotations

import typer

from ppa.cli.utils import (
    console,
    error_block,
    info,
    next_step,
    run_cmd,
    success,
)
from ppa.config import DEFAULT_APP_NAME, DEFAULT_NAMESPACE, DEPLOY_DIR, PROJECT_DIR


def _run_apply_pipeline(
    app_name: str = DEFAULT_APP_NAME,
    namespace: str = DEFAULT_NAMESPACE,
    skip_build: bool = False,
    dry_run: bool = False,
    keep_hpa: bool = True,
    rollback: bool = False,
) -> None:
    """Core deploy pipeline (extracted so train --apply can call it).

    Steps:
    1. Build operator image (Minikube Docker)
    2. Deploy manifests to cluster
    3. Wait for rollout
    4. Verify operator is running
    """
    from ppa.cli.commands.operator import (
        _apply_manifest,
        _check_minikube,
        _get_docker_env,
    )

    if not _check_minikube():
        error_block(
            "Minikube not running",
            cause="Cannot reach Minikube Docker daemon",
            fix="minikube start",
        )
        raise typer.Exit(1)

    image = "ppa-operator:latest"

    if not skip_build:
        import subprocess

        info("Building operator image...")
        dockerfile = PROJECT_DIR / "src" / "ppa" / "operator" / "Dockerfile"
        if not dockerfile.exists():
            error_block(
                "Dockerfile not found",
                cause=str(dockerfile),
                fix="Ensure the operator Dockerfile exists at src/ppa/operator/Dockerfile",
            )
            raise typer.Exit(1)

        env = _get_docker_env()
        cmd = ["docker", "build", "-t", image, "-f", str(dockerfile), str(PROJECT_DIR)]

        if dry_run:
            console.print(f"  [dim]Would run: {' '.join(cmd)}[/]")
        else:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if result.returncode != 0:
                error_block(
                    "Docker build failed",
                    cause=result.stderr[:200] if result.stderr else "Unknown error",
                    fix="ppa apply --skip-build",
                )
                raise typer.Exit(1)
            success("Operator image built")

    if dry_run:
        console.print()
        console.print("  Dry run — no resources modified.")
        console.print(f"  To deploy:  [bold]ppa apply --app-name {app_name}[/]")
        return

    # Deploy manifests
    info("Applying operator manifests...")
    _apply_manifest(DEPLOY_DIR / "rbac.yaml", namespace, "rbac.yaml")
    _apply_manifest(
        DEPLOY_DIR / "operator-deployment.yaml", namespace, "operator-deployment.yaml"
    )

    run_cmd(
        [
            "kubectl",
            "set",
            "image",
            "deployment/ppa-operator",
            f"operator={image}",
            f"--namespace={namespace}",
        ],
        title="Setting operator image",
    )

    # Rollout
    info("Waiting for rollout...")
    run_cmd(
        [
            "kubectl",
            "rollout",
            "status",
            "deployment/ppa-operator",
            f"--namespace={namespace}",
            "--timeout=120s",
        ],
        title="Operator rollout",
    )

    success("Operator deployed")


def apply_cmd(
    app_name: str = typer.Option(
        DEFAULT_APP_NAME, "--app-name", "-a", help="Target application name."
    ),
    namespace: str = typer.Option(
        DEFAULT_NAMESPACE, "--namespace", "-n", help="Kubernetes namespace."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be deployed, no changes."
    ),
    rollback: bool = typer.Option(
        False, "--rollback", help="Roll back to previous operator version."
    ),
    skip_build: bool = typer.Option(
        False, "--skip-build", help="Skip Docker image build, use existing image."
    ),
    keep_hpa: bool = typer.Option(
        True, "--keep-hpa/--delete-hpa", help="Keep HPA alongside PPA."
    ),
    watch_after: bool = typer.Option(
        False, "--watch", help="Open live dashboard after deploy."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt."
    ),
) -> None:
    """Deploy autoscaler with the current trained model.

    Builds the operator image, deploys manifests, and verifies rollout.

    \b
    EXAMPLES
      ppa apply                         # build + deploy with confirmation
      ppa apply --dry-run               # preview what would happen
      ppa apply --skip-build            # redeploy without rebuilding
      ppa apply --yes                   # skip confirmation
      ppa apply --watch                 # deploy then open dashboard

    \b
    REQUIRES
      • Minikube running — check: ppa status
      • Model trained — check: ppa model evaluate
    """
    console.print()
    console.print(f"  Deploying PPA for [bold]{app_name}[/] in [bold]{namespace}[/]")
    console.print()

    if not dry_run and not yes:
        if not typer.confirm("  Proceed with deployment?"):
            console.print("  Cancelled.  No changes made.")
            raise typer.Exit()

    _run_apply_pipeline(
        app_name=app_name,
        namespace=namespace,
        skip_build=skip_build,
        dry_run=dry_run,
        keep_hpa=keep_hpa,
        rollback=rollback,
    )

    if dry_run:
        raise typer.Exit()

    console.print()
    success(f"PPA deployed for {app_name}")

    if watch_after:
        from ppa.cli.commands.watch import watch_cmd

        watch_cmd(interval=15, app=app_name)
    else:
        next_step(f"ppa watch --app {app_name}", "observe live scaling")
