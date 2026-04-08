"""Input validation helpers for common CLI arguments."""

from __future__ import annotations

import re
from pathlib import Path

from ppa.cli.utilities.errors import KubernetesError, PrometheusError, ValidationError

# Regex patterns

APP_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

# Validators

def validate_app_name(name: str) -> str:
    """Validate app name (DNS subdomain format).

    Args:
        name: Application name

    Returns:
        Validated name

    Raises:
        ValidationError: If name is invalid

    Examples:
        >>> validate_app_name("my-app")
        'my-app'
        >>> validate_app_name("my_app")  # Invalid: underscores not allowed
        # Raises ValidationError
    """
    if not name:
        raise ValidationError(
            "App name cannot be empty",
            suggestion="Provide a DNS-compliant app name (e.g., 'my-app')",
        )

    if len(name) > 63:
        raise ValidationError(
            f"App name too long: {len(name)} > 63 characters",
            suggestion="Shorten to 63 characters or fewer",
        )

    if not APP_NAME_PATTERN.match(name):
        raise ValidationError(
            f"Invalid app name: {name}",
            context={"pattern": "^[a-z0-9]([a-z0-9-]{{0,61}}[a-z0-9])?$"},
            suggestion=(
                "Use lowercase alphanumerics and hyphens only "
                "(not starting/ending with hyphen)"
            ),
        )

    return name


def validate_namespace(ns: str) -> str:
    """Validate Kubernetes namespace name.

    Args:
        ns: Namespace name

    Returns:
        Validated namespace

    Raises:
        ValidationError: If namespace is invalid
    """
    if not ns:
        raise ValidationError(
            "Namespace cannot be empty",
            suggestion="Provide a valid Kubernetes namespace",
        )

    if len(ns) > 63:
        raise ValidationError(
            f"Namespace too long: {len(ns)} > 63 characters",
            suggestion="Shorten to 63 characters or fewer",
        )

    if not NAMESPACE_PATTERN.match(ns):
        raise ValidationError(
            f"Invalid namespace: {ns}",
            suggestion=(
                "Use lowercase alphanumerics and hyphens only "
                "(not starting/ending with hyphen)"
            ),
        )

    return ns


def validate_horizon(hours: int) -> int:
    """Validate prediction horizon in hours.

    Args:
        hours: Horizon in hours

    Returns:
        Validated horizon

    Raises:
        ValidationError: If horizon is out of range
    """
    if not isinstance(hours, int):
        try:
            hours = int(hours)
        except ValueError as e:
            raise ValidationError(f"Horizon must be an integer, got: {hours}") from e

    if hours < 1:
        raise ValidationError(
            f"Horizon must be >= 1 hour, got: {hours}",
            suggestion="Use a positive integer (e.g., 3 for 3-hour forecast)",
        )

    if hours > 24:
        raise ValidationError(
            f"Horizon too large: {hours} hours (max 24)",
            suggestion="Use a value between 1 and 24 hours",
        )

    return hours


def validate_filepath(filepath: str | Path, must_exist: bool = False) -> Path:
    """Validate file path.

    Args:
        filepath: Path to validate
        must_exist: If True, file must exist

    Returns:
        Validated Path object

    Raises:
        ValidationError: If path is invalid or doesn't exist (when must_exist=True)
    """
    try:
        path = Path(filepath)
    except (TypeError, ValueError) as e:
        raise ValidationError(
            f"Invalid file path: {filepath}",
            suggestion="Provide a valid file path",
        ) from e

    if must_exist and not path.exists():
        raise ValidationError(
            f"File not found: {path}",
            context={"path": str(path)},
            suggestion="Create the file or provide a path that exists",
        )

    return path


def validate_kubernetes_connection(kubeconfig: str | Path | None = None) -> None:
    """Validate that kubectl can connect to Kubernetes.

    Args:
        kubeconfig: Path to kubeconfig (optional)

    Raises:
        KubernetesError: If connection fails
    """
    import subprocess

    cmd = ["kubectl", "cluster-info"]
    if kubeconfig:
        cmd.extend(["--kubeconfig", str(kubeconfig)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise KubernetesError(
                "Cannot connect to Kubernetes cluster",
                context={"error": result.stderr.strip()},
                suggestion=(
                    "Check kubeconfig (export KUBECONFIG=/path/to/config) "
                    "or start a cluster (minikube start)"
                ),
            )
    except subprocess.TimeoutExpired as e:
        raise KubernetesError(
            "Kubernetes API timeout (10 seconds)",
            suggestion="Check your cluster is running and kubeconfig is valid",
        ) from e
    except FileNotFoundError as e:
        raise KubernetesError(
            "kubectl not found in PATH",
            suggestion="Install kubectl: https://kubernetes.io/docs/tasks/tools/ or add to PATH",
        ) from e


def validate_prometheus_connection(url: str) -> None:
    """Validate that Prometheus is accessible.

    Args:
        url: Prometheus base URL (e.g., http://localhost:9090)

    Raises:
        PrometheusError: If connection fails
    """
    import requests

    try:
        response = requests.get(f"{url}/-/ready", timeout=5)
        if response.status_code != 200:
            raise PrometheusError(
                f"Prometheus health check failed: {response.status_code}",
                context={"url": url},
                suggestion="Check Prometheus is running and accessible at this URL",
            )
    except requests.ConnectionError as e:
        raise PrometheusError(
            f"Cannot connect to Prometheus at {url}",
            context={"error": str(e)},
            suggestion=(
                "Ensure Prometheus is running and accessible "
                "(check port forwarding if remote)"
            ),
        ) from e
    except requests.Timeout as e:
        raise PrometheusError(
            f"Prometheus connection timeout (5 seconds) at {url}",
            suggestion="Check network connectivity and Prometheus is responsive",
        ) from e
    except Exception as e:
        raise PrometheusError(
            f"Unexpected error connecting to Prometheus: {e}",
            suggestion="Check URL format (should be http://host:port)",
        ) from e
