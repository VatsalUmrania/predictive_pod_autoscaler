"""Centralized configuration management for PPA.

Single source of truth for all configuration. Use `from ppa.config import ...` to access.

Usage:
    from ppa.config import get_config, PROMETHEUS_URL, DEPLOY_DIR
    from ppa.config import OperatorConfig, ModelConfig
"""

from __future__ import annotations

import os
import platform
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# ── Global config singleton ──────────────────────────────────────────────────
_global_config: Optional["Config"] = None


def get_config() -> "Config":
    """Get the current configuration (lazy-loaded from env on first call)."""
    global _global_config
    if _global_config is None:
        _global_config = Config.from_env()
    return _global_config


def set_config(config: "Config") -> None:
    """Override configuration (for testing)."""
    global _global_config
    _global_config = config


def reset_config() -> None:
    """Reset config to None (reloads from env on next get_config)."""
    global _global_config
    _global_config = None


# ── Paths (module-level constants for convenience) ────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parents[2]
SESSION_FILE = PROJECT_DIR / ".ppa_session"

DEPLOY_DIR = PROJECT_DIR / "deploy"
DATA_DIR = PROJECT_DIR / "data"
MODEL_DIR = DATA_DIR / "models"
TRAINING_DATA_DIR = DATA_DIR / "training-data"
CHAMPION_DIR = DATA_DIR / "champions"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
TESTS_DIR = PROJECT_DIR / "tests"


# ── Network ports ─────────────────────────────────────────────────────────────
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "9090"))
GRAFANA_PORT = int(os.getenv("GRAFANA_PORT", "3000"))
APP_PORT = int(os.getenv("APP_PORT", "8080"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9091"))
PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    os.getenv("PPA_PROMETHEUS_URL", "http://prometheus:9090"),
)


def get_prometheus_url(cr_spec_url: str | None = None) -> str:
    """Get Prometheus URL, preferring CR-specific URL over global default."""
    if cr_spec_url:
        return cr_spec_url
    return PROMETHEUS_URL


# ── Minikube defaults ────────────────────────────────────────────────────────
MINIKUBE_CPUS = int(os.getenv("MINIKUBE_CPUS", "4"))
MINIKUBE_MEMORY = int(os.getenv("MINIKUBE_MEMORY", "8192"))
MINIKUBE_DISK_SIZE = os.getenv("MINIKUBE_DISK_SIZE", "20g")
MINIKUBE_K8S_VERSION = os.getenv("MINIKUBE_K8S_VERSION", "v1.28.3")


def get_minikube_driver() -> str:
    """Get Minikube driver based on platform or PPA_MINIKUBE_DRIVER env."""
    override = os.getenv("PPA_MINIKUBE_DRIVER")
    if override:
        return override
    system = platform.system().lower()
    if system == "linux":
        return "kvm2"
    elif system == "darwin":
        return "docker"
    elif system == "windows":
        return "docker"
    return ""


MINIKUBE_DRIVER = get_minikube_driver()


# ── Default values ────────────────────────────────────────────────────────────
DEFAULT_APP_NAME = os.getenv("PPA_DEFAULT_APP_NAME", "test-app")
DEFAULT_NAMESPACE = os.getenv("PPA_NAMESPACE", os.getenv("NAMESPACE", "default"))
DEFAULT_HORIZON = os.getenv("PPA_DEFAULT_HORIZON", "rps_t10m")
DEFAULT_CSV = os.getenv("PPA_DEFAULT_CSV", str(TRAINING_DATA_DIR / "training_data_v2.csv"))
DEFAULT_LOOKBACK = int(os.getenv("PPA_LOOKBACK_STEPS", "60"))
DEFAULT_EPOCHS = int(os.getenv("PPA_EPOCHS", "50"))
NAMESPACE = "default"
CONTAINER_NAME = "test-app"

# ── Operator defaults (flat constants for backward compat) ────────────────────
TIMER_INTERVAL = 30
INITIAL_DELAY = 60
STABILIZATION_STEPS = 2
STABILIZATION_TOLERANCE = 0.5
LOOKBACK_STEPS = 60
PROM_FAILURE_THRESHOLD = 10
DEFAULT_CAPACITY_PER_POD = 50
DEFAULT_MIN_REPLICAS = 2
DEFAULT_MAX_REPLICAS = 20
DEFAULT_SCALE_UP_RATE = 2.0
DEFAULT_SCALE_DOWN_RATE = 0.5
DEFAULT_MODEL_DIR = "/models"


# ── Dataclass configs ─────────────────────────────────────────────────────────


@dataclass
class PrometheusConfig:
    """Prometheus connection configuration."""

    url: str = "http://prometheus:9090"
    timeout: int = 2
    failure_threshold: int = 10

    @classmethod
    def from_env(cls) -> "PrometheusConfig":
        return cls(
            url=os.getenv("PROMETHEUS_URL", "http://prometheus:9090"),
            timeout=int(os.getenv("PROM_TIMEOUT", "2")),
            failure_threshold=int(os.getenv("PPA_PROM_FAILURE_THRESHOLD", "10")),
        )


@dataclass
class OperatorConfig:
    """Kubernetes operator runtime configuration."""

    namespace: str = "default"
    timer_interval: int = 30
    initial_delay: int = 60
    stabilization_steps: int = 2
    stabilization_tolerance: float = 0.5
    lookback_steps: int = 60
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "OperatorConfig":
        return cls(
            namespace=os.getenv("PPA_NAMESPACE", "default"),
            timer_interval=int(os.getenv("PPA_TIMER_INTERVAL", "30")),
            initial_delay=int(os.getenv("PPA_INITIAL_DELAY", "60")),
            stabilization_steps=int(os.getenv("PPA_STABILIZATION_STEPS", "2")),
            stabilization_tolerance=float(os.getenv("PPA_STABILIZATION_TOLERANCE", "0.5")),
            lookback_steps=int(os.getenv("PPA_LOOKBACK_STEPS", "60")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


@dataclass
class ModelConfig:
    """ML model training and inference configuration."""

    model_dir: str = "/models"
    default_horizon: str = "rps_t10m"
    lookback_steps: int = 60

    @classmethod
    def from_env(cls) -> "ModelConfig":
        return cls(
            model_dir=os.getenv("PPA_MODEL_DIR", "/models"),
            default_horizon=os.getenv("PPA_DEFAULT_HORIZON", "rps_t10m"),
            lookback_steps=int(os.getenv("PPA_LOOKBACK_STEPS", "60")),
        )


@dataclass
class ScalingConfig:
    """Replica scaling policy configuration."""

    min_replicas: int = 2
    max_replicas: int = 20
    scale_up_rate: float = 2.0
    scale_down_rate: float = 0.5
    capacity_per_pod: int = 50
    safety_factor: float = 1.10

    @classmethod
    def from_env(cls) -> "ScalingConfig":
        return cls(
            min_replicas=int(os.getenv("PPA_MIN_REPLICAS", "2")),
            max_replicas=int(os.getenv("PPA_MAX_REPLICAS", "20")),
            scale_up_rate=float(os.getenv("PPA_SCALE_UP_RATE", "2.0")),
            scale_down_rate=float(os.getenv("PPA_SCALE_DOWN_RATE", "0.5")),
            capacity_per_pod=int(os.getenv("PPA_CAPACITY_PER_POD", "50")),
            safety_factor=float(os.getenv("PPA_SAFETY_FACTOR", "1.10")),
        )


@dataclass
class DataflowConfig:
    """Data collection and training pipeline configuration."""

    target_app: str = "test-app"
    namespace: str = "default"
    container_name: str = "test-app"
    training_data_dir: str = "data/training-data"

    @classmethod
    def from_env(cls) -> "DataflowConfig":
        return cls(
            target_app=os.getenv("TARGET_APP", "test-app"),
            namespace=os.getenv("NAMESPACE", "default"),
            container_name=os.getenv("CONTAINER_NAME", "test-app"),
            training_data_dir=os.getenv("TRAINING_DATA_DIR", "data/training-data"),
        )


@dataclass
class CLIConfig:
    """CLI and local development configuration."""

    prometheus_port: int = 9090
    grafana_port: int = 3000
    app_port: int = 8080
    metrics_port: int = 9091
    minikube_cpus: int = 4
    minikube_memory: int = 8192
    minikube_disk_size: str = "20g"
    minikube_k8s_version: str = "v1.28.3"

    @classmethod
    def from_env(cls) -> "CLIConfig":
        return cls(
            prometheus_port=int(os.getenv("PROMETHEUS_PORT", "9090")),
            grafana_port=int(os.getenv("GRAFANA_PORT", "3000")),
            app_port=int(os.getenv("APP_PORT", "8080")),
            metrics_port=int(os.getenv("METRICS_PORT", "9091")),
            minikube_cpus=int(os.getenv("MINIKUBE_CPUS", "4")),
            minikube_memory=int(os.getenv("MINIKUBE_MEMORY", "8192")),
            minikube_disk_size=os.getenv("MINIKUBE_DISK_SIZE", "20g"),
            minikube_k8s_version=os.getenv("MINIKUBE_K8S_VERSION", "v1.28.3"),
        )


@dataclass
class PathsConfig:
    """Project directory paths (computed at runtime)."""

    project_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])
    deploy_dir: Path = field(init=False)
    model_dir: Path = field(init=False)
    data_dir: Path = field(init=False)
    training_data_dir: Path = field(init=False)
    champion_dir: Path = field(init=False)
    artifacts_dir: Path = field(init=False)
    scripts_dir: Path = field(init=False)
    tests_dir: Path = field(init=False)

    def __post_init__(self):
        self.deploy_dir = self.project_dir / "deploy"
        self.data_dir = self.project_dir / "data"
        self.model_dir = self.data_dir / "models"
        self.training_data_dir = self.data_dir / "training-data"
        self.champion_dir = self.data_dir / "champions"
        self.artifacts_dir = self.data_dir / "artifacts"
        self.scripts_dir = self.project_dir / "scripts"
        self.tests_dir = self.project_dir / "tests"

    @classmethod
    def from_env(cls) -> "PathsConfig":
        project_dir_str = os.getenv("PROJECT_DIR")
        if project_dir_str:
            return cls(project_dir=Path(project_dir_str))
        return cls()


# ── Root configuration ───────────────────────────────────────────────────────


@dataclass
class Config:
    """Root configuration combining all sub-configs."""

    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig.from_env)
    operator: OperatorConfig = field(default_factory=OperatorConfig.from_env)
    model: ModelConfig = field(default_factory=ModelConfig.from_env)
    scaling: ScalingConfig = field(default_factory=ScalingConfig.from_env)
    dataflow: DataflowConfig = field(default_factory=DataflowConfig.from_env)
    cli: CLIConfig = field(default_factory=CLIConfig.from_env)
    paths: PathsConfig = field(default_factory=PathsConfig.from_env)

    def to_dict(self) -> dict:
        return {
            "prometheus": self.prometheus.__dict__,
            "operator": self.operator.__dict__,
            "model": self.model.__dict__,
            "scaling": self.scaling.__dict__,
            "dataflow": self.dataflow.__dict__,
            "cli": self.cli.__dict__,
            "paths": {
                "project_dir": str(self.paths.project_dir),
                "deploy_dir": str(self.paths.deploy_dir),
                "model_dir": str(self.paths.model_dir),
                "data_dir": str(self.paths.data_dir),
                "training_data_dir": str(self.paths.training_data_dir),
                "champion_dir": str(self.paths.champion_dir),
                "artifacts_dir": str(self.paths.artifacts_dir),
                "scripts_dir": str(self.paths.scripts_dir),
                "tests_dir": str(self.paths.tests_dir),
            },
        }

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            prometheus=PrometheusConfig.from_env(),
            operator=OperatorConfig.from_env(),
            model=ModelConfig.from_env(),
            scaling=ScalingConfig.from_env(),
            dataflow=DataflowConfig.from_env(),
            cli=CLIConfig.from_env(),
            paths=PathsConfig.from_env(),
        )


# ── Exception classes ────────────────────────────────────────────────────────


class FeatureVectorException(Exception):
    """Raised when feature extraction fails (Prometheus unavailable, network issues, etc.)."""

    pass


# ── Rich theme ───────────────────────────────────────────────────────────────

if TYPE_CHECKING:
    from rich.theme import Theme

    PPA_THEME: Theme
else:
    try:
        from rich.theme import Theme

        PPA_THEME = Theme(
            {
                "success": "bold green",
                "warning": "bold yellow",
                "error": "bold red",
                "info": "bold cyan",
                "step": "bold blue",
                "metric": "bold",
                "heading": "bold blue",
                "dim": "italic",
                "accent": "bold cyan",
            }
        )
    except ImportError:
        PPA_THEME = None


def get_banner() -> str:
    from ppa import __version__

    return f"[bold cyan]PPA[/] [bold blue]v{__version__}[/] [dim]• Predictive Pod Autoscaler[/]"


__all__ = [
    # Singleton functions
    "get_config",
    "set_config",
    "reset_config",
    "get_prometheus_url",
    "get_minikube_driver",
    # Dataclasses
    "Config",
    "PrometheusConfig",
    "OperatorConfig",
    "ModelConfig",
    "ScalingConfig",
    "DataflowConfig",
    "CLIConfig",
    "PathsConfig",
    # Exceptions
    "FeatureVectorException",
    # Theme
    "PPA_THEME",
    "get_banner",
    # Module-level constants
    "PROMETHEUS_PORT",
    "PROMETHEUS_URL",
    "GRAFANA_PORT",
    "APP_PORT",
    "METRICS_PORT",
    "MINIKUBE_CPUS",
    "MINIKUBE_MEMORY",
    "MINIKUBE_DISK_SIZE",
    "MINIKUBE_K8S_VERSION",
    "MINIKUBE_DRIVER",
    "DEFAULT_APP_NAME",
    "DEFAULT_NAMESPACE",
    "DEFAULT_HORIZON",
    "DEFAULT_CSV",
    "DEFAULT_LOOKBACK",
    "DEFAULT_EPOCHS",
    # Paths
    "PROJECT_DIR",
    "SESSION_FILE",
    "DEPLOY_DIR",
    "MODEL_DIR",
    "DATA_DIR",
    "TRAINING_DATA_DIR",
    "CHAMPION_DIR",
    "ARTIFACTS_DIR",
    "SCRIPTS_DIR",
    "TESTS_DIR",
    # Operator defaults
    "NAMESPACE",
    "CONTAINER_NAME",
    "TIMER_INTERVAL",
    "INITIAL_DELAY",
    "STABILIZATION_STEPS",
    "STABILIZATION_TOLERANCE",
    "LOOKBACK_STEPS",
    "PROM_FAILURE_THRESHOLD",
    "DEFAULT_CAPACITY_PER_POD",
    "DEFAULT_MIN_REPLICAS",
    "DEFAULT_MAX_REPLICAS",
    "DEFAULT_SCALE_UP_RATE",
    "DEFAULT_SCALE_DOWN_RATE",
    "DEFAULT_MODEL_DIR",
]
