"""Shared CLI configuration — paths, ports, theme, banner."""

import os
import platform
from pathlib import Path

from rich.theme import Theme

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_DIR / "deploy"
SESSION_FILE = PROJECT_DIR / ".ppa_session"
MODEL_DIR = PROJECT_DIR / "model"
DATA_DIR = PROJECT_DIR / "data-collection"
TRAINING_DATA_DIR = DATA_DIR / "training-data"
CHAMPION_DIR = MODEL_DIR / "champions"
ARTIFACTS_DIR = MODEL_DIR / "artifacts"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
TESTS_DIR = PROJECT_DIR / "tests"

# ── Network ──────────────────────────────────────────────────────────────────
PROMETHEUS_PORT = 9090
GRAFANA_PORT = 3000
APP_PORT = 8080
METRICS_PORT = 9091
PROMETHEUS_URL = f"http://localhost:{PROMETHEUS_PORT}"

# ── Minikube Driver (Cross-Platform) ───────────────────────────────────────
def _get_default_minikube_driver() -> str:
    """Auto-detect appropriate Minikube driver based on platform."""
    # Allow override via environment variable
    override = os.environ.get("PPA_MINIKUBE_DRIVER")
    if override:
        return override

    system = platform.system().lower()
    if system == "linux":
        return "kvm2"  # Best performance on Linux
    elif system == "darwin":  # macOS
        return "docker"  # Docker Desktop most common on macOS
    elif system == "windows":
        return "docker"  # Docker Desktop or Podman on Windows
    else:
        return ""  # Let Minikube auto-detect

MINIKUBE_DRIVER = _get_default_minikube_driver()
MINIKUBE_CPUS = 4
MINIKUBE_MEMORY = 8192
MINIKUBE_DISK_SIZE = "20g"
MINIKUBE_K8S_VERSION = "v1.28.3"

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_APP_NAME = "test-app"
DEFAULT_NAMESPACE = "default"
DEFAULT_HORIZON = "rps_t10m"
DEFAULT_CSV = str(TRAINING_DATA_DIR / "training_data_v2.csv")
DEFAULT_LOOKBACK = 60
DEFAULT_EPOCHS = 50

# ── Rich Theme ───────────────────────────────────────────────────────────────
PPA_THEME = Theme({
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "info": "bold cyan",
    "step": "bold blue",
    "metric": "bold",
    "heading": "bold blue",
    "dim": "italic",
    "accent": "bold cyan",
})

# ── Minimal Header ───────────────────────────────────────────────────────────
def get_banner() -> str:
    from cli import __version__
    return f"[bold cyan]PPA[/] [bold blue]v{__version__}[/] [dim]• Predictive Pod Autoscaler[/]"
