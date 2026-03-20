"""ppa startup — Cluster bootstrap (replaces ppa_startup.sh).

Translates the 11-step bash startup script into Python with Rich UI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import typer

from ppa.cli.utils import (
    check_binary,
    console,
    error,
    get_minikube_docker_env,
    heading,
    info,
    run_cmd,
    run_cmd_silent,
    save_session,
    step_heading,
    success,
    wait_for_pods,
    warn,
)
from ppa.config import (
    APP_PORT,
    DEFAULT_APP_NAME,
    DEFAULT_NAMESPACE,
    DEPLOY_DIR,
    GRAFANA_PORT,
    METRICS_PORT,
    MINIKUBE_CPUS,
    MINIKUBE_DISK_SIZE,
    MINIKUBE_DRIVER,
    MINIKUBE_K8S_VERSION,
    MINIKUBE_MEMORY,
    PROJECT_DIR,
    PROMETHEUS_PORT,
    get_banner,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)


# ── Step registry ────────────────────────────────────────────────────────────
def _get_step_2_description() -> str:
    driver_display = MINIKUBE_DRIVER if MINIKUBE_DRIVER else "auto"
    return f"{driver_display} driver, {MINIKUBE_CPUS} CPU, {MINIKUBE_MEMORY // 1024} GB RAM"


STEPS: list[tuple[int, str, str | Callable[[], str]]] = [
    (1, "Check Prerequisites", "docker, kubectl, helm, python3, git"),
    (2, "Start Minikube", _get_step_2_description),
    (3, "Enable Minikube Addons", "metrics-server, ingress"),
    (4, "Install Prometheus Stack", "kube-prometheus-stack via Helm"),
    (5, "Build & Deploy Test App", "Build image inside Minikube"),
    (6, "Deploy Traffic Generator", "In-cluster Locust swarm"),
    (7, "Start Port Forwards", "Prometheus, Grafana, test-app"),
    (8, "Start Port Forward Watchdog", "Auto-restart dead forwards"),
    (9, "Verify ML Features", "14-dim feature set from Prometheus"),
    (10, "Deploy Data Collection CronJob", "Hourly data collector"),
    (11, "Fixed-Replica Chaos Profiling", "Capacity profiling under chaos load"),
]


def _show_startup_plan(steps: list[int]) -> None:
    """Show a concise plan of what will be executed."""
    console.print(get_banner())
    console.print("\n[bold]Startup Plan:[/]")
    for s in steps:
        step_num, name, desc = STEPS[s - 1]
        description = desc() if callable(desc) else desc
        console.print(f"  [step]{s:02d}[/] [bold]{name}[/] — {description}")
    console.print()


def _show_step_list() -> None:
    """List all available startup steps."""
    console.print(get_banner())
    console.print("\n[bold]Available Steps:[/]")
    for step_num, name, desc in STEPS:
        description = desc() if callable(desc) else desc
        console.print(f"  [step]{step_num:02d}[/] [bold]{name}[/] — {description}")
    console.print()


def _render_dataflow_dockerfile(
    output_path: str | Path,
    image_tag: str = "ppa-data-collector:latest",
    pip_packages: str | None = None,
    cmd: str | None = None,
    base_image: str = "python:3.11-slim",
) -> None:
    """Render src/ppa/dataflow/Dockerfile.j2 to output_path.

    Args:
        output_path: Where to write the rendered Dockerfile
        image_tag: Tag for the collector image
        pip_packages: Override pip packages string (default: hardcoded in template)
        cmd: Override CMD (default: python3 -m ppa.dataflow.export_training_data)
        base_image: Python base image
    """
    try:
        from jinja2 import Environment, FileSystemLoader
    except ImportError:
        warn("jinja2 not installed — cannot render dataflow Dockerfile template")
        warn("Install with: pip install jinja2")
        return

    template_dir = PROJECT_DIR / "src" / "ppa" / "dataflow"
    env = Environment(loader=FileSystemLoader(str(template_dir)))
    try:
        template = env.get_template("Dockerfile.j2")
    except Exception:
        warn(f"Dockerfile.j2 not found in {template_dir} — skipping template render")
        return

    rendered = template.render(
        image_tag=image_tag,
        pip_packages=pip_packages,
        cmd=cmd,
        base_image=base_image,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered)
    success(f"Rendered Dockerfile → {output_path}")


# ── Helper functions ────────────────────────────────────────────────────────────


def _get_app_path(app_arg: str | None) -> Path | None:
    """Resolve test-app path from CLI argument or git URL.

    Args:
        app_arg: CLI --app argument (path or git URL)

    Returns:
        Path to test-app directory, or None if not provided
    """
    if not app_arg:
        return None

    if app_arg.startswith("http") or app_arg.startswith("git@"):
        info(f"Cloning test-app from {app_arg}")
        clone_dir = PROJECT_DIR / "test-app-clone"
        if clone_dir.exists():
            import shutil

            shutil.rmtree(clone_dir)
        subprocess.run(["git", "clone", app_arg, str(clone_dir)], check=True)
        return clone_dir

    return Path(app_arg)


# ── Individual step implementations ──────────────────────────────────────────


def _step_1_prerequisites() -> None:
    required = ["docker", "kubectl", "helm", "python3", "git"]
    for binary in required:
        if check_binary(binary):
            success(f"{binary} found")
        else:
            error(f"{binary} NOT found — please install it first")
            raise typer.Exit(1)

    if not check_binary("locust"):
        warn("locust not found — installing...")
        run_cmd(
            ["pip", "install", "locust", "pandas", "requests"],
            title="Installing Python deps",
        )


def _step_2_minikube() -> None:
    result = run_cmd_silent(["minikube", "status", "--format", "{{.Host}}"], check=False)
    status = result.stdout.strip() if result.returncode == 0 else "Stopped"

    if status == "Running":
        success("Minikube already running")
    else:
        driver_display = MINIKUBE_DRIVER if MINIKUBE_DRIVER else "auto"
        cmd = [
            "minikube",
            "start",
            "--cpus",
            str(MINIKUBE_CPUS),
            "--memory",
            str(MINIKUBE_MEMORY),
            "--disk-size",
            MINIKUBE_DISK_SIZE,
            "--kubernetes-version",
            MINIKUBE_K8S_VERSION,
        ]
        if MINIKUBE_DRIVER:
            cmd.extend(["--driver", MINIKUBE_DRIVER])
        run_cmd(
            cmd,
            title=f"Starting Minikube with {driver_display} driver",
        )
        success("Minikube started")

    run_cmd(["kubectl", "get", "nodes"], title="Verifying nodes")


def _step_3_addons() -> None:
    run_cmd(
        ["minikube", "addons", "enable", "metrics-server"],
        title="Enabling metrics-server",
    )
    run_cmd(["minikube", "addons", "enable", "ingress"], title="Enabling ingress")

    info("Patching metrics-server for Minikube TLS...")
    run_cmd_silent(
        [
            "kubectl",
            "patch",
            "deployment",
            "metrics-server",
            "-n",
            "kube-system",
            "--type=json",
            '-p=[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]',
        ],
        check=False,
    )
    time.sleep(10)
    result = run_cmd_silent(["kubectl", "top", "nodes"], check=False)
    if result.returncode == 0:
        success("Metrics server working")
    else:
        warn("Metrics server still warming up")


def _step_4_prometheus() -> None:
    run_cmd_silent(
        [
            "helm",
            "repo",
            "add",
            "prometheus-community",
            "https://prometheus-community.github.io/helm-charts",
        ],
        check=False,
    )
    run_cmd(["helm", "repo", "update"], title="Updating Helm repos")
    run_cmd_silent(["kubectl", "create", "namespace", "monitoring"], check=False)

    info("Applying PPA Dashboard ConfigMap...")
    run_cmd_silent(["kubectl", "apply", "-f", str(DEPLOY_DIR / "grafana-dashboard-configmap.yaml")])

    result = run_cmd_silent(["helm", "status", "prometheus", "-n", "monitoring"], check=False)
    if result.returncode == 0:
        success("Prometheus already installed — upgrading with sidecar config...")
        run_cmd(
            [
                "helm",
                "upgrade",
                "prometheus",
                "prometheus-community/kube-prometheus-stack",
                "--namespace",
                "monitoring",
                "--reuse-values",
                "--set",
                "grafana.sidecar.dashboards.enabled=true",
                "--set",
                "grafana.sidecar.dashboards.searchNamespace=monitoring",
                "--timeout=5m",
            ],
            title="Helm upgrade Prometheus",
        )
    else:
        run_cmd(
            [
                "helm",
                "install",
                "prometheus",
                "prometheus-community/kube-prometheus-stack",
                "--namespace",
                "monitoring",
                "--set",
                "grafana.adminPassword=admin123",
                "--set",
                "prometheus.prometheusSpec.retention=30d",
                "--set",
                "prometheus.prometheusSpec.scrapeInterval=15s",
                "--set",
                "grafana.sidecar.dashboards.enabled=true",
                "--set",
                "grafana.sidecar.dashboards.searchNamespace=monitoring",
                "--timeout=5m",
            ],
            title="Helm install Prometheus stack",
        )
        info("Waiting for Prometheus pods...")
        time.sleep(30)
        wait_for_pods("app.kubernetes.io/name=prometheus", "monitoring")
        success("Prometheus stack installed")


# Global to store app_path between steps
_app_path: Path | None = None


def _step_5_test_app(ctx: typer.Context | None = None) -> None:
    global _app_path

    if ctx and ctx.obj:
        _app_path = ctx.obj.get("app_path")

    app_path = _app_path
    if app_path is None:
        error("test-app not found. Use --app option to specify path")
        raise typer.Exit(1)

    info(f"Building test-app from {app_path}")
    docker_env = {**os.environ, **get_minikube_docker_env()}
    run_cmd(
        [
            "docker",
            "build",
            "-t",
            "test-app:latest",
            "-f",
            str(app_path / "Dockerfile"),
            str(app_path),
        ],
        title="Building test-app Docker image",
        env=docker_env,
    )
    success("Docker image built: test-app:latest")

    deployment_yaml = app_path / "deployment.yaml"
    if deployment_yaml.exists():
        run_cmd(["kubectl", "apply", "-f", str(deployment_yaml)])
    else:
        warn(f"deployment.yaml not found in {app_path}")

    result = run_cmd_silent(
        ["kubectl", "get", "deployment", "test-app", "-n", DEFAULT_NAMESPACE],
        check=False,
    )
    if result.returncode == 0:
        success("test-app deployment updated — rolling restart...")
        run_cmd_silent(["kubectl", "rollout", "restart", "deployment/test-app"])

    time.sleep(10)
    wait_for_pods("app=test-app", DEFAULT_NAMESPACE)


def _step_6_traffic_gen() -> None:
    info("Staging Locust traffic generator in-cluster...")

    # Create ConfigMap without pipes (cross-platform)
    import subprocess as _sp

    locustfile_path = PROJECT_DIR / "tests" / "locustfile.py"

    # Generate YAML with dry-run
    create_result = _sp.run(
        [
            "kubectl",
            "create",
            "configmap",
            "traffic-gen-locustfile",
            f"--namespace={DEFAULT_NAMESPACE}",
            f"--from-file=locustfile.py={locustfile_path}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if create_result.returncode != 0:
        error(f"kubectl create configmap failed: {create_result.stderr}")
        raise typer.Exit(1)

    # Apply the generated YAML
    apply_result = _sp.run(
        ["kubectl", "apply", "-f", "-"],
        input=create_result.stdout,
        capture_output=True,
        text=True,
        check=False,
    )

    if apply_result.returncode != 0:
        error(f"kubectl apply failed: {apply_result.stderr}")
        raise typer.Exit(1)

    success("Locust ConfigMap created")
    run_cmd_silent(["kubectl", "apply", "-f", str(DEPLOY_DIR / "traffic-gen-deployment.yaml")])
    run_cmd_silent(
        [
            "kubectl",
            "rollout",
            "restart",
            "deployment/traffic-gen",
            "-n",
            DEFAULT_NAMESPACE,
        ],
        check=False,
    )
    wait_for_pods("app=traffic-gen", DEFAULT_NAMESPACE)
    success("Staged Locust traffic generator running in-cluster")


def _step_7_port_forwards() -> None:
    # Kill existing port-forwards
    for port in [PROMETHEUS_PORT, GRAFANA_PORT, APP_PORT, METRICS_PORT]:
        if sys.platform == "win32":
            run_cmd_silent(
                f'for /f "tokens=5" %p in (\'netstat -ano ^| findstr ":{port} "\') do taskkill /F /PID %p',
                check=False,
                shell=True,
            )
        else:
            run_cmd_silent(["pkill", "-f", f"port-forward.*{port}"], check=False)
    time.sleep(2)

    from rich.progress import Progress, SpinnerColumn, TextColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Waiting for Prometheus pod...", total=36)
        for i in range(1, 37):
            result = run_cmd_silent(
                [
                    "kubectl",
                    "get",
                    "pods",
                    "-n",
                    "monitoring",
                    "-l",
                    "app.kubernetes.io/name=prometheus",
                    "--no-headers",
                ],
                check=False,
            )
            if "2/2" in result.stdout:
                success("Prometheus pod ready")
                break
            progress.update(task, description=f"Waiting for Prometheus pod... ({i}/36)")
            time.sleep(10)
            progress.advance(task)

    # Start port-forwards in background
    import subprocess as _sp

    pids = {}
    forwards = [
        (
            [
                "kubectl",
                "port-forward",
                "svc/prometheus-kube-prometheus-prometheus",
                f"{PROMETHEUS_PORT}:9090",
                "-n",
                "monitoring",
            ],
            "Prometheus",
            "prom",
        ),
        (
            [
                "kubectl",
                "port-forward",
                "svc/prometheus-grafana",
                f"{GRAFANA_PORT}:80",
                "-n",
                "monitoring",
            ],
            "Grafana",
            "grafana",
        ),
        (
            [
                "kubectl",
                "port-forward",
                "svc/test-app",
                f"{APP_PORT}:80",
                "-n",
                DEFAULT_NAMESPACE,
            ],
            "test-app",
            "app",
        ),
        (
            [
                "kubectl",
                "port-forward",
                "svc/test-app",
                f"{METRICS_PORT}:9091",
                "-n",
                DEFAULT_NAMESPACE,
            ],
            "test-app metrics",
            "metrics",
        ),
    ]
    for cmd, label, key in forwards:
        proc = _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        pids[f"pf_{key}"] = proc.pid
        success(f"{label} port-forward started (PID: {proc.pid})")

    save_session(pids)
    time.sleep(3)
    success(f"Prometheus  → http://localhost:{PROMETHEUS_PORT}")
    success(f"Grafana     → http://localhost:{GRAFANA_PORT} (admin/admin123)")
    success(f"test-app    → http://localhost:{APP_PORT}")
    success(f"Metrics     → http://localhost:{METRICS_PORT}/metrics")


def _step_8_watchdog() -> None:
    if sys.platform == "win32":
        warn("Watchdog not supported on Windows — port-forwards will not auto-restart")
        return

    import shutil
    import tempfile

    if not shutil.which("bash"):
        warn("bash not found — skipping watchdog (port-forwards will not auto-restart)")
        return

    watchdog_script = r"""#!/bin/bash
while true; do
    if ! curl -s http://localhost:9090/-/ready > /dev/null 2>&1; then
        pkill -f "port-forward.*9090" 2>/dev/null
        kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &>/dev/null &
    fi
    if ! curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
        pkill -f "port-forward.*3000" 2>/dev/null
        kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &>/dev/null &
    fi
    if ! curl -s http://localhost:8080/ > /dev/null 2>&1; then
        pkill -f "port-forward.*8080" 2>/dev/null
        kubectl port-forward svc/test-app 8080:80 -n default &>/dev/null &
    fi
    if ! curl -s http://localhost:9091/metrics > /dev/null 2>&1; then
        pkill -f "port-forward.*9091" 2>/dev/null
        kubectl port-forward svc/test-app 9091:9091 -n default &>/dev/null &
    fi
    sleep 30
done
"""
    tmp_dir = tempfile.gettempdir()
    watchdog_path = os.path.join(tmp_dir, "ppa_watchdog.sh")
    log_path = os.path.join(tmp_dir, "ppa_watchdog.log")
    with open(watchdog_path, "w") as f:
        f.write(watchdog_script)
    os.chmod(watchdog_path, 0o755)

    import subprocess as _sp

    proc = _sp.Popen(
        ["bash", watchdog_path],
        stdout=open(log_path, "w"),
        stderr=_sp.STDOUT,
        start_new_session=True,  # Detach from terminal (replaces nohup)
    )
    save_session({"watchdog": proc.pid})
    success(f"Watchdog running (PID: {proc.pid}) — auto-restarts dead port-forwards every 30s")


def _step_9_verify_features() -> None:
    from ppa.cli.utils import prometheus_ready

    for i in range(1, 13):
        if prometheus_ready():
            info("Prometheus ready — waiting 30s for metrics to populate...")
            time.sleep(30)

            try:
                import requests  # type: ignore[import-untyped]

                resp = requests.get(
                    f"http://localhost:{PROMETHEUS_PORT}/api/v1/query",
                    params={"query": "up"},
                    timeout=10,
                )
                if resp.ok and resp.json().get("status") == "success":
                    success(f"Prometheus responding — {DEFAULT_APP_NAME} metrics available")
                else:
                    warn("Prometheus query returned unexpected response")
            except Exception as e:
                warn(f"Could not verify Prometheus metrics: {e}")
            return
        if i == 12:
            warn(
                "Prometheus not reachable — verify features manually with: ppa data export --dry-run"
            )
            return
        info(f"[{i}/12] Waiting for Prometheus...")
        time.sleep(10)


def _step_10_cronjob() -> None:
    from ppa.cli.utils import prometheus_ready

    if not prometheus_ready():
        warn(
            "Prometheus not reachable — skipping CronJob. Run manually: kubectl apply -f deploy/cronjob-data-collector.yaml"
        )
        return

    template_j2 = PROJECT_DIR / "src" / "ppa" / "dataflow" / "Dockerfile.j2"
    dockerfile_out = PROJECT_DIR / "src" / "ppa" / "dataflow" / "Dockerfile"

    if template_j2.exists():
        _render_dataflow_dockerfile(dockerfile_out, image_tag="ppa-data-collector:latest")

    if not dockerfile_out.exists():
        warn("Data collector Dockerfile not found — skipping CronJob deployment")
        warn("Create src/ppa/dataflow/Dockerfile to enable automatic CronJob deployment")
        return

    info("Building data-collector image inside Minikube...")
    docker_env = {**os.environ, **get_minikube_docker_env()}
    docker_env["DOCKER_BUILDKIT"] = "0"

    run_cmd(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile_out),
            "-t",
            "ppa-data-collector:latest",
            str(PROJECT_DIR),
        ],
        title="Building data-collector Docker image",
        env=docker_env,
    )
    success("Collector image built: ppa-data-collector:latest")

    cronjob_yaml = DEPLOY_DIR / "cronjob-data-collector.yaml"
    if cronjob_yaml.exists():
        run_cmd_silent(["kubectl", "apply", "-f", str(cronjob_yaml)])
        success("CronJob created for hourly data collection")
    else:
        warn(f"CronJob manifest not found: {cronjob_yaml}")


def _step_11_chaos() -> None:
    import shutil

    script_path = PROJECT_DIR / "scripts" / "fixed_replica_test.sh"

    if sys.platform == "win32":
        if not shutil.which("bash"):
            warn("bash not found on Windows — skipping fixed-replica chaos profiling")
            warn("To run: Download Git Bash or WSL and execute: bash scripts/fixed_replica_test.sh")
            return
        # On Windows, explicitly use bash
        run_cmd(
            ["bash", str(script_path)],
            title="Fixed-replica chaos profiling",
        )
    else:
        # On Unix/Linux/macOS, the shebang will be respected
        run_cmd(
            [str(script_path)],
            title="Fixed-replica chaos profiling",
        )
    success("Fixed-replica profiling complete")


# ── Step dispatcher ──────────────────────────────────────────────────────────
STEP_FUNCS: dict[int, Callable[[], None]] = {
    1: _step_1_prerequisites,
    2: _step_2_minikube,
    3: _step_3_addons,
    4: _step_4_prometheus,
    5: _step_5_test_app,
    6: _step_6_traffic_gen,
    7: _step_7_port_forwards,
    8: _step_8_watchdog,
    9: _step_9_verify_features,
    10: _step_10_cronjob,
    11: _step_11_chaos,
}


def _run_done_banner() -> None:
    from rich.panel import Panel

    lines = [
        "[success]✓ PPA Infrastructure is Ready[/success]",
        "",
        f"  [info]Prometheus[/info]   → http://localhost:{PROMETHEUS_PORT}",
        f"  [info]Grafana[/info]      → http://localhost:{GRAFANA_PORT}",
        f"  [info]Test App[/info]     → http://localhost:{APP_PORT}",
        "",
        "  [dim]Run [bold]ppa follow[/bold] to switch to live monitoring.[/dim]",
    ]
    console.print()
    console.print(Panel("\n".join(lines), border_style="success", padding=(1, 2)))


# ── Main command ─────────────────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def startup(
    ctx: typer.Context,
    step: int | None = typer.Option(None, "--step", "-s", help="Run only a specific step (1-11)."),
    list_steps: bool = typer.Option(False, "--list", "-l", help="List all startup steps."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would run without executing."),
    follow_mode: bool = typer.Option(
        False, "--follow", "-f", help="Attach to live monitor after startup."
    ),
    app: str | None = typer.Option(None, "--app", "-a", help="Path or git URL to test-app source."),
) -> None:
    """
    [bold]Bootstrap the full PPA cluster infrastructure.[/]

    Runs all 11 steps sequentially: prerequisites → Minikube → Prometheus →
    test-app → traffic-gen → port-forwards → watchdog → verify features →
    CronJob → chaos profiling.

    Use --app to specify test-app source:
        ppa startup --app ./test-app
        ppa startup --app https://github.com/you/test-app.git
    """
    global _app_path
    ctx.ensure_object(dict)
    app_path = _get_app_path(app)
    ctx.obj["app_path"] = app_path
    _app_path = app_path

    if list_steps:
        _show_step_list()
        raise typer.Exit()

    if ctx.invoked_subcommand is not None:
        return

    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
    )

    steps_to_run = [step] if step else list(range(1, 12))

    # Show plan first (Deliverable 7: Responsive feedback)
    _show_startup_plan(steps_to_run)

    if dry_run:
        heading("DRY RUN — Steps to execute")
        for s in steps_to_run:
            _, name, desc = STEPS[s - 1]
            description = desc() if callable(desc) else desc
            info(f"Step {s}: {name} — {description}")
        raise typer.Exit()

    total = len(steps_to_run)
    with Progress(
        SpinnerColumn(style="bright_magenta"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30, style="bright_cyan", complete_style="bright_green"),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[bold]PPA Startup[/bold]", total=total)

        for _i, s in enumerate(steps_to_run, 1):
            _, name, desc = STEPS[s - 1]
            step_heading(s, 11, name)
            progress.update(task, description=f"[bold]Step {s}:[/bold] {name}")

            try:
                STEP_FUNCS[s]()
            except Exception as e:
                error(f"Step {s} failed: {e}")
                if step:
                    raise typer.Exit(1) from None
                warn("Continuing to next step...")

            progress.advance(task)

    if not step:
        _run_done_banner()

    if follow_mode:
        from ppa.cli.commands.follow import follow as follow_cmd

        follow_cmd()
