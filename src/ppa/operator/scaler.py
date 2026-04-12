# operator/scaler.py — Scaling orchestration adapter
"""Adapts scaling decisions to Kubernetes operations.

Bridges domain logic (calculate_replicas in ppa.domain.scaling)
with infrastructure (K8s deployment operations in ppa.infrastructure.kubernetes).

This module is responsible for:
- Calculating desired replicas (re-exported from domain for backward compatibility)
- Orchestrating deployment scaling (delegates to infrastructure.kubernetes)
- Error handling and logging
"""

# Re-export domain scaling functions for backward compatibility
from ppa.domain.scaling import calculate_replicas, calculate_replicas_fixed, calculate_replicas_old

# Private re-exports for backward compatibility with old private function names
# (in case any internal code still uses these)
# Import infrastructure adapters
from ppa.infrastructure.kubernetes import scale_deployment as _scale_deployment_impl


def scale_deployment(deployment: str, replicas: int, namespace: str = "default") -> bool:
    """Patch the Deployment's replica count.

    Public wrapper around infrastructure.kubernetes.scale_deployment.
    Orchestrates K8s scaling operations with proper error handling and logging.

    Args:
        deployment: Deployment name
        replicas: Desired replica count
        namespace: Kubernetes namespace (default: "default")

    Returns:
        True if patch succeeded, False if K8s API unavailable

    Example:
        >>> success = scale_deployment("api-server", replicas=10, namespace="prod")
        >>> if success:
        ...     print("Scaling applied")
    """
    return _scale_deployment_impl(deployment, replicas, namespace)


__all__ = [
    "calculate_replicas",
    "calculate_replicas_fixed",
    "calculate_replicas_old",
    "scale_deployment",
]
