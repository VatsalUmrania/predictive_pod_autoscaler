"""Shared CLI configuration — paths, ports, theme, banner."""

from pathlib import Path

from rich.theme import Theme

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_DIR / "deploy"
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

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_APP_NAME = "test-app"
DEFAULT_NAMESPACE = "default"
DEFAULT_HORIZON = "rps_t10m"
DEFAULT_CSV = str(TRAINING_DATA_DIR / "training_data_v2.csv")
DEFAULT_LOOKBACK = 60
DEFAULT_EPOCHS = 50

# ── Rich Theme ───────────────────────────────────────────────────────────────
PPA_THEME = Theme({
    "success": "bold #22c55e",
    "warning": "bold #f59e0b",
    "error": "bold #ef4444",
    "info": "bold #38bdf8",
    "step": "bold #a78bfa",
    "metric": "bold #e5e7eb",
    "heading": "bold #60a5fa",
    "dim": "#9ca3af",
    "accent": "bold #22d3ee",
})

# ── ASCII Banner ─────────────────────────────────────────────────────────────
ASCII_BANNER = r"""[bold bright_cyan]
  ╔═══════════════════════════════════════════════════════╗
  ║   ____  ____   _                                      ║
  ║  |  _ \|  _ \ / \      Predictive Pod Autoscaler      ║
  ║  | |_) | |_) / _ \     ─────────────────────────      ║
  ║  |  __/|  __/ ___ \    ML-driven K8s scaling          ║
  ║  |_|   |_| /_/   \_\   Typer + Rich CLI v{version}    ║
  ║                                                       ║
  ╚═══════════════════════════════════════════════════════╝
[/bold bright_cyan]"""


def get_banner() -> str:
    from cli import __version__
    return ASCII_BANNER.format(version=__version__)
