"""PPA CLI core infrastructure — errors, validation, config, suggestions, progress."""

from ppa.cli.core.config import CLIConfig, load_cli_config, save_cli_config
from ppa.cli.core.errors import (
    ConfigError,
    KubernetesError,
    PPAError,
    PrometheusError,
    ValidationError,
)
from ppa.cli.core.progress import Progress, spinner
from ppa.cli.core.suggestions import format_error_with_suggestion, suggest_fix
from ppa.cli.core.validators import (
    validate_app_name,
    validate_filepath,
    validate_horizon,
    validate_kubernetes_connection,
    validate_namespace,
    validate_prometheus_connection,
)

__all__ = [
    "PPAError",
    "ValidationError",
    "ConfigError",
    "KubernetesError",
    "PrometheusError",
    "validate_app_name",
    "validate_namespace",
    "validate_horizon",
    "validate_filepath",
    "validate_kubernetes_connection",
    "validate_prometheus_connection",
    "CLIConfig",
    "load_cli_config",
    "save_cli_config",
    "suggest_fix",
    "format_error_with_suggestion",
    "Progress",
    "spinner",
]
