"""Kubernetes helpers for PPA CLI.

This module consolidates K8s utilities from the old cli/k8s structure.
"""

# Re-export everything from original K8s modules
from ppa.cli.k8s.client import get_apps_v1, get_client, get_core_v1
from ppa.cli.k8s.kubectl import cp, exec_cmd, mkdir, validate_cluster
from ppa.cli.k8s.pod import create_loader_pod, delete_pod, unique_pod_name, wait_for_ready
from ppa.cli.k8s.pvc import ensure_exists

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

