"""
Unit tests for Universal Kubernetes Dynamic Provider Tool Bridge.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from nexus.tools.k8s.universal import (
    K8sGetLogsInput,
    K8sInspectResourceInput,
    K8sMutateResourceInput,
    execute_k8s_get_logs,
    execute_k8s_inspect,
    execute_k8s_mutate,
)


def test_k8s_inspect_resource():
    mock_dyn = MagicMock()
    mock_resource = MagicMock()
    mock_dyn.resources.get.return_value = mock_resource
    mock_obj = MagicMock()
    mock_obj.to_dict.return_value = {
        "metadata": {"name": "redis-0", "namespace": "prod"},
        "status": {"phase": "Pending"},
    }
    mock_resource.get.return_value = mock_obj

    inp = K8sInspectResourceInput(
        api_version="apps/v1",
        kind="StatefulSet",
        name="redis-0",
        namespace="prod",
    )
    res = json.loads(execute_k8s_inspect(inp, dyn_client=mock_dyn))
    mock_dyn.resources.get.assert_called_with(api_version="apps/v1", kind="StatefulSet")
    assert res["status"]["phase"] == "Pending"


def test_k8s_get_logs():
    mock_core = MagicMock()
    mock_core.read_namespaced_pod_log.return_value = "fatal error: out of memory"

    inp = K8sGetLogsInput(pod_name="api-pod-123", namespace="default", previous=True)
    res = execute_k8s_get_logs(inp, core_v1=mock_core)
    assert "fatal error: out of memory" in res
    mock_core.read_namespaced_pod_log.assert_called_with(
        name="api-pod-123",
        namespace="default",
        container=None,
        tail_lines=100,
        previous=True,
    )


def test_k8s_mutate_operations():
    mock_dyn = MagicMock()
    mock_resource = MagicMock()
    mock_dyn.resources.get.return_value = mock_resource

    # 1. Rollout restart
    mock_resource.patch.return_value = {"metadata": {"name": "auth-service"}}
    inp_restart = K8sMutateResourceInput(
        api_version="apps/v1",
        kind="Deployment",
        name="auth-service",
        namespace="prod",
        mutation_type="rollout_restart",
    )
    res = json.loads(execute_k8s_mutate(inp_restart, dyn_client=mock_dyn))
    assert res["metadata"]["name"] == "auth-service"
    call_kwargs = mock_resource.patch.call_args.kwargs
    assert "restartedAt" in str(call_kwargs["body"])

    # 2. Scale replicas
    inp_scale = K8sMutateResourceInput(
        api_version="apps/v1",
        kind="StatefulSet",
        name="kafka",
        namespace="prod",
        mutation_type="scale_replicas",
        patch_body={"replicas": 5},
    )
    execute_k8s_mutate(inp_scale, dyn_client=mock_dyn)
    assert mock_resource.patch.call_args.kwargs["body"] == {"spec": {"replicas": 5}}
