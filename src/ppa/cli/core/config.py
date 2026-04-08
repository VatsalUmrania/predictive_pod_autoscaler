"""CLI configuration management (YAML-based, ~/.ppa/cli.yaml)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from ppa.cli.core.errors import ConfigError

# Config paths


def get_cli_config_dir() -> Path:
    """Get PPA CLI config directory (~/.ppa)."""
    config_dir = Path.home() / ".ppa"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cli_config_path() -> Path:
    """Get PPA CLI config file path (~/.ppa/cli.yaml)."""
    return get_cli_config_dir() / "cli.yaml"


# Config dataclass


@dataclass
class CLIConfig:
    """PPA CLI configuration.

    Attributes:
        default_app_name: Default app name for commands (if not specified)
        default_namespace: Default Kubernetes namespace
        default_horizon: Default prediction horizon in hours
        prometheus_url: Prometheus base URL (http://localhost:9090)
        kubeconfig: Path to kubeconfig file (optional, uses KUBECONFIG env var if not set)
        interactive: Enable interactive mode for complex commands
        color: Enable colored output (auto-detect by default)
        debug: Enable debug mode and verbose logging
    """

    default_app_name: str = "demo-app"
    default_namespace: str = "default"
    default_horizon: int = 3
    prometheus_url: str = "http://localhost:9090"
    kubeconfig: str | None = None
    interactive: bool = False
    color: bool | None = None  # None means auto-detect TTY
    debug: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> CLIConfig:
        """Create config from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        """Convert config to dictionary."""
        return asdict(self)


# Config I/O


def load_cli_config(path: Path | None = None) -> CLIConfig:
    """Load CLI config from YAML file.

    Args:
        path: Config file path (default: ~/.ppa/cli.yaml)

    Returns:
        Loaded CLIConfig (with defaults if file doesn't exist)

    Raises:
        ConfigError: If file is invalid YAML
    """
    if path is None:
        path = get_cli_config_path()

    if not path.exists():
        return CLIConfig()

    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return CLIConfig.from_dict(data)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Invalid YAML in config: {path}",
            context={"error": str(e)},
            suggestion="Check file format or remove to regenerate",
        ) from e
    except OSError as e:
        raise ConfigError(
            f"Cannot read config file: {path}",
            context={"error": str(e)},
            suggestion="Check file permissions",
        ) from e


def save_cli_config(config: CLIConfig, path: Path | None = None) -> None:
    """Save CLI config to YAML file.

    Args:
        config: CLIConfig to save
        path: Config file path (default: ~/.ppa/cli.yaml)

    Raises:
        ConfigError: If write fails
    """
    if path is None:
        path = get_cli_config_path()

    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(path, "w") as f:
            yaml.dump(config.to_dict(), f, default_flow_style=False, sort_keys=False)
    except OSError as e:
        raise ConfigError(
            f"Cannot write config file: {path}",
            context={"error": str(e)},
            suggestion="Check directory permissions",
        ) from e
