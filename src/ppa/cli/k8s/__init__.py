"""Kubernetes helpers — DEPRECATED for backward compatibility.

DEPRECATED: Import directly from the submodules instead:
    from ppa.cli.k8s.client import get_client, get_core_v1, get_apps_v1
    from ppa.cli.k8s.kubectl import cp, exec_cmd, mkdir, validate_cluster
    from ppa.cli.k8s.pod import create_loader_pod, delete_pod, unique_pod_name, wait_for_ready
    from ppa.cli.k8s.pvc import ensure_exists

This file is kept for backward compatibility but should not re-export from
ppa.cli.utilities.kubernetes to avoid circular imports.
"""
