"""Kubernetes helpers — re-exported from ppa.cli.utilities.kubernetes for backward compatibility.

DEPRECATED: Import directly from ppa.cli.utilities.kubernetes instead:
    from ppa.cli.utilities.kubernetes import get_client, cp, exec_cmd, mkdir
"""

from ppa.cli.utilities.kubernetes import (
    cp,
    create_loader_pod,
    delete_pod,
    ensure_exists,
    exec_cmd,
    get_apps_v1,
    get_client,
    get_core_v1,
    mkdir,
    unique_pod_name,
    validate_cluster,
    wait_for_ready,
)

__all__ = [
    "get_client",
    "get_core_v1",
    "get_apps_v1",
    "cp",
    "exec_cmd",
    "mkdir",
    "validate_cluster",
    "create_loader_pod",
    "wait_for_ready",
    "delete_pod",
    "unique_pod_name",
    "ensure_exists",
]
