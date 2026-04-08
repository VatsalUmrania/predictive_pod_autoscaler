"""Integrations for external services: Prometheus, Minikube, and more."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from ppa.config import PROMETHEUS_URL

if TYPE_CHECKING:
    pass

__all__ = [
    "query_prometheus",
    "prometheus_ready",
    "get_minikube_docker_env",
]

# Prometheus Helpers

def query_prometheus(query: str, url: str = PROMETHEUS_URL) -> str | None:
    """Execute an instant query against Prometheus and return scalar value.

    Sends a PromQL query to Prometheus and extracts the scalar result value.
    Returns None if the query fails or returns no results.

    Args:
        query: PromQL query expression (e.g., "up{job='prometheus'}")
        url: Prometheus API base URL (default from config)

    Returns:
        String representation of scalar result, or None if query fails/empty

    Examples:
        >>> value = query_prometheus("rate(requests_total[5m])")
        >>> if value:
        ...     print(f"Request rate: {value} req/s")
    """
    import requests

    try:
        resp = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if results:
            return str(round(float(results[0]["value"][1]), 2))
    except Exception:
        pass
    return None


def prometheus_ready(url: str = PROMETHEUS_URL) -> bool:
    """Check if Prometheus server is ready and accepting queries.

    Sends a readiness probe to the Prometheus /-/ready endpoint to verify
    the server is operational and ready to accept queries.

    Args:
        url: Prometheus API base URL (default from config)

    Returns:
        True if Prometheus is ready, False if unreachable or not ready

    Examples:
        >>> if not prometheus_ready():
        ...     error("Prometheus is not reachable. Start it with: ppa startup")
    """
    import requests

    try:
        resp = requests.get(f"{url}/-/ready", timeout=5)
        return "Ready" in resp.text
    except Exception:
        return False

# Minikube Helpers

def get_minikube_docker_env() -> dict[str, str]:
    """Extract Docker environment variables from Minikube.

    Queries Minikube for Docker daemon environment variables and parses them
    into a dictionary for use when building Docker images inside Minikube.
    Cross-platform compatible without shell evaluation.

    Returns:
        Dictionary of Docker environment variables (e.g., DOCKER_HOST, DOCKER_TLS_VERIFY)

    Examples:
        >>> env = get_minikube_docker_env()
        >>> env.update(os.environ)
        >>> result = run_cmd(["docker", "build", "."], env=env)
    """
    result = subprocess.run(
        ["minikube", "docker-env", "--shell", "none"],
        capture_output=True,
        text=True,
        check=False,
    )
    env_vars: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env_vars[key.strip()] = value.strip().strip('"').strip("'")
    return env_vars
