"""ppa startup — Cluster bootstrap (replaces ppa_startup.sh).

Translates the 11-step bash startup script into Python with Rich UI.
"""

from __future__ import annotations

import os
import sys
import time

import typer
from rich.table import Table

from cli.config import (
    APP_PORT,
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
from cli.utils import (
    check_binary,
    console,
    error,
    heading,
    info,
    get_minikube_docker_env,
    kubectl,
    run_cmd,
    run_cmd_silent,
    step_heading,
    success,
    save_session,
    wait_for_pods,
    warn,
)

app = typer.Typer(rich_markup_mode="rich", invoke_without_command=True)

# ── Step registry ────────────────────────────────────────────────────────────
def _get_step_2_description() -> str:
    driver_display = MINIKUBE_DRIVER if MINIKUBE_DRIVER else "auto"
    return f"{driver_display} driver, {MINIKUBE_CPUS} CPU, {MINIKUBE_MEMORY // 1024} GB RAM"

STEPS: list[tuple[int, str, str | callable]] = [
    (1,  "Check Prerequisites",               "docker, kubectl, helm, python3, git"),
    (2,  "Start Minikube",                     _get_step_2_description),
    (3,  "Enable Minikube Addons",             "metrics-server, ingress"),
    (4,  "Install Prometheus Stack",           "kube-prometheus-stack via Helm"),
    (5,  "Build & Deploy Test App",            "Build image inside Minikube"),
    (6,  "Deploy Traffic Generator",           "In-cluster Locust swarm"),
    (7,  "Start Port Forwards",               "Prometheus, Grafana, test-app"),
    (8,  "Start Port Forward Watchdog",        "Auto-restart dead forwards"),
    (9,  "Verify ML Features",                 "14-dim feature set from Prometheus"),
    (10, "Deploy Data Collection CronJob",     "Hourly data collector"),
    (11, "Fixed-Replica Chaos Profiling",      "Capacity profiling under chaos load"),
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
        run_cmd(["pip", "install", "locust", "pandas", "requests"], title="Installing Python deps")


def _step_2_minikube() -> None:
    result = run_cmd_silent(["minikube", "status", "--format", "{{.Host}}"], check=False)
    status = result.stdout.strip() if result.returncode == 0 else "Stopped"

    if status == "Running":
        success("Minikube already running")
    else:
        driver_display = MINIKUBE_DRIVER if MINIKUBE_DRIVER else "auto"
        cmd = [
            "minikube", "start",
            "--cpus", str(MINIKUBE_CPUS),
            "--memory", str(MINIKUBE_MEMORY),
            "--disk-size", MINIKUBE_DISK_SIZE,
            "--kubernetes-version", MINIKUBE_K8S_VERSION,
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
    run_cmd(["minikube", "addons", "enable", "metrics-server"], title="Enabling metrics-server")
    run_cmd(["minikube", "addons", "enable", "ingress"], title="Enabling ingress")

    info("Patching metrics-server for Minikube TLS...")
    run_cmd_silent(
        [
            "kubectl", "patch", "deployment", "metrics-server",
            "-n", "kube-system",
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
    run_cmd_silent(["helm", "repo", "add", "prometheus-community", "https://prometheus-community.github.io/helm-charts"], check=False)
    run_cmd(["helm", "repo", "update"], title="Updating Helm repos")
    run_cmd_silent(["kubectl", "create", "namespace", "monitoring"], check=False)

    info("Applying PPA Dashboard ConfigMap...")
    run_cmd_silent(["kubectl", "apply", "-f", str(DEPLOY_DIR / "grafana-dashboard-configmap.yaml")])

    result = run_cmd_silent(["helm", "status", "prometheus", "-n", "monitoring"], check=False)
    if result.returncode == 0:
        success("Prometheus already installed — upgrading with sidecar config...")
        run_cmd(
            [
                "helm", "upgrade", "prometheus", "prometheus-community/kube-prometheus-stack",
                "--namespace", "monitoring", "--reuse-values",
                "--set", "grafana.sidecar.dashboards.enabled=true",
                "--set", "grafana.sidecar.dashboards.searchNamespace=monitoring",
                "--timeout=5m",
            ],
            title="Helm upgrade Prometheus",
        )
    else:
        run_cmd(
            [
                "helm", "install", "prometheus", "prometheus-community/kube-prometheus-stack",
                "--namespace", "monitoring",
                "--set", "grafana.adminPassword=admin123",
                "--set", "prometheus.prometheusSpec.retention=30d",
                "--set", "prometheus.prometheusSpec.scrapeInterval=15s",
                "--set", "grafana.sidecar.dashboards.enabled=true",
                "--set", "grafana.sidecar.dashboards.searchNamespace=monitoring",
                "--timeout=5m",
            ],
            title="Helm install Prometheus stack",
        )
        info("Waiting for Prometheus pods...")
        time.sleep(30)
        wait_for_pods("app.kubernetes.io/name=prometheus", "monitoring")
        success("Prometheus stack installed")


def _step_5_test_app() -> None:
    info("Building test-app image inside Minikube...")
    docker_env = {**os.environ, **get_minikube_docker_env()}
    run_cmd(
        [
            "docker", "build", "-t", "test-app:latest",
            str(PROJECT_DIR / "data-collection" / "test-app"),
        ],
        title="Building test-app Docker image",
        env=docker_env,
    )
    success("Docker image built: test-app:latest")

    run_cmd_silent(["kubectl", "apply", "-f", str(PROJECT_DIR / "data-collection" / "test-app-deployment.yaml")])

    result = run_cmd_silent(["kubectl", "get", "deployment", "test-app", "-n", DEFAULT_NAMESPACE], check=False)
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
            "kubectl", "create", "configmap", "traffic-gen-locustfile",
            f"--namespace={DEFAULT_NAMESPACE}",
            f"--from-file=locustfile.py={locustfile_path}",
            "--dry-run=client", "-o", "yaml",
        ],
        capture_output=True, text=True, check=False,
    )

    if create_result.returncode != 0:
        error(f"kubectl create configmap failed: {create_result.stderr}")
        raise typer.Exit(1)

    # Apply the generated YAML
    apply_result = _sp.run(
        ["kubectl", "apply", "-f", "-"],
        input=create_result.stdout,
        capture_output=True, text=True, check=False,
    )

    if apply_result.returncode != 0:
        error(f"kubectl apply failed: {apply_result.stderr}")
        raise typer.Exit(1)

    success("Locust ConfigMap created")
    run_cmd_silent(["kubectl", "apply", "-f", str(DEPLOY_DIR / "traffic-gen-deployment.yaml")])
    run_cmd_silent(["kubectl", "rollout", "restart", "deployment/traffic-gen", "-n", DEFAULT_NAMESPACE], check=False)
    wait_for_pods("app=traffic-gen", DEFAULT_NAMESPACE)
    success("Staged Locust traffic generator running in-cluster")


def _step_7_port_forwards() -> None:
    # Kill existing port-forwards
    for port in [PROMETHEUS_PORT, GRAFANA_PORT, APP_PORT, METRICS_PORT]:
        if sys.platform == "win32":
            run_cmd_silent(
                f'for /f "tokens=5" %p in (\'netstat -ano ^| findstr ":{port} "\') do taskkill /F /PID %p',
                check=False, shell=True,
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
                ["kubectl", "get", "pods", "-n", "monitoring", "-l", "app.kubernetes.io/name=prometheus", "--no-headers"],
                check=False,
            )
            if "2/2" in result.stdout:
                success(f"Prometheus pod ready")
                break
            progress.update(task, description=f"Waiting for Prometheus pod... ({i}/36)")
            time.sleep(10)
            progress.advance(task)

    # Start port-forwards in background
    import subprocess as _sp
    pids = {}
    forwards = [
        (["kubectl", "port-forward", "svc/prometheus-kube-prometheus-prometheus", f"{PROMETHEUS_PORT}:9090", "-n", "monitoring"], "Prometheus", "prom"),
        (["kubectl", "port-forward", "svc/prometheus-grafana", f"{GRAFANA_PORT}:80", "-n", "monitoring"], "Grafana", "grafana"),
        (["kubectl", "port-forward", "svc/test-app", f"{APP_PORT}:80", "-n", DEFAULT_NAMESPACE], "test-app", "app"),
        (["kubectl", "port-forward", "svc/test-app", f"{METRICS_PORT}:9091", "-n", DEFAULT_NAMESPACE], "test-app metrics", "metrics"),
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
    from cli.utils import prometheus_ready

    for i in range(1, 13):
        if prometheus_ready():
            info("Prometheus ready — waiting 30s for metrics to populate...")
            time.sleep(30)
            run_cmd(
                ["python3", str(PROJECT_DIR / "data-collection" / "verify_features.py")],
                title="Verifying 14 ML features",
                check=False,
            )
            return
        if i == 12:
            warn("Prometheus not reachable — run manually: python3 data-collection/verify_features.py")
            return
        info(f"[{i}/12] Waiting for Prometheus...")
        time.sleep(10)


def _step_10_cronjob() -> None:
    from cli.utils import prometheus_ready

    if not prometheus_ready():
        warn("Prometheus not reachable — skipping CronJob. Run manually: kubectl apply -f deploy/cronjob-data-collector.yaml")
        return

    info("Building data-collector image inside Minikube...")
    docker_env = {**os.environ, **get_minikube_docker_env()}
    run_cmd(
        [
            "docker", "build",
            "-f", str(PROJECT_DIR / "data-collection" / "Dockerfile"),
            "-t", "ppa-data-collector:latest",
            str(PROJECT_DIR),
        ],
        title="Building data-collector Docker image",
        env=docker_env,
    )
    success("Collector image built: ppa-data-collector:latest")
    run_cmd_silent(["kubectl", "apply", "-f", str(DEPLOY_DIR / "cronjob-data-collector.yaml")])
    success("CronJob created for hourly data collection")


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
STEP_FUNCS = {
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
    follow_mode: bool = typer.Option(False, "--follow", "-f", help="Attach to live monitor after startup."),
) -> None:
    """
    [bold]Bootstrap the full PPA cluster infrastructure.[/]

    Runs all 11 steps sequentially: prerequisites → Minikube → Prometheus →
    test-app → traffic-gen → port-forwards → watchdog → verify features →
    CronJob → chaos profiling.
    """
    if list_steps:
        _show_step_list()
        raise typer.Exit()

    if ctx.invoked_subcommand is not None:
        return

    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

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

        for i, s in enumerate(steps_to_run, 1):
            _, name, desc = STEPS[s - 1]
            step_heading(s, 11, name)
            progress.update(task, description=f"[bold]Step {s}:[/bold] {name}")

            try:
                STEP_FUNCS[s]()
            except Exception as e:
                error(f"Step {s} failed: {e}")
                if step:
                    raise typer.Exit(1)
                warn("Continuing to next step...")

            progress.advance(task)

    if not step:
        _run_done_banner()

    if follow_mode:
        from cli.commands.follow import follow as follow_cmd
        follow_cmd()
