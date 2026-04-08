"""CLI utilities: error handling, progress tracking, validation, and K8s integration.

Consolidates all CLI helper modules in one place for easier navigation and imports.
"""

from ppa.cli.utilities.errors import (
    ConfigError,
    KubernetesError,
    PPAError,
    PrometheusError,
    ValidationError,
)
from ppa.cli.utilities.progress import Progress, spinner
from ppa.cli.utilities.suggestions import format_error_with_suggestion, suggest_fix
from ppa.cli.utilities.validators import (
    validate_app_name,
    validate_filepath,
    validate_horizon,
    validate_kubernetes_connection,
    validate_namespace,
    validate_prometheus_connection,
)

__all__ = [
    "PPAError",
    "ConfigError",
    "KubernetesError",
    "PrometheusError",
    "ValidationError",
    "Progress",
    "spinner",
    "format_error_with_suggestion",
    "suggest_fix",
    "validate_app_name",
    "validate_filepath",
    "validate_horizon",
    "validate_kubernetes_connection",
    "validate_namespace",
    "validate_prometheus_connection",
]
