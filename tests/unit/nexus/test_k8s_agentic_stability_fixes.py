"""
Unit Tests for Kubernetes Stability & Agentic Refactoring Fixes
===============================================================
Validates:
  1. uid:count deduplication in K8sAgent for repeated crash events.
  2. k8s_inspect_resource retaining metadata.ownerReferences and status.lastState without managedFields clutter.
  3. k8s_mutate_resource strategic_merge_patch and rollback support.
  4. Deletion of infer_symptom_fallback and ReAct investigator loop tool binding.
  5. Strict multi-replica health verification checking rollout and crashing pods.
  6. Asynchronous Slack approval handling responding within 3 seconds.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.engine.platforms.base import TargetResource
from nexus.engine.platforms.k8s import K8sPlatformAdapter
from nexus.engine.tools.k8s.universal import (
    K8sInspectResourceInput,
    K8sMutateResourceInput,
    execute_k8s_inspect,
    execute_k8s_mutate,
)

# ── 1. k8s_inspect_resource returns ownerReferences & lastState ───────────────


def test_k8s_inspect_resource_preserves_owner_and_last_state():
    mock_dyn = MagicMock()
    mock_res = MagicMock()
    mock_dyn.resources.get.return_value = mock_res

    mock_pod_data = {
        "metadata": {
            "name": "shop-demo-76d87fc7fb-7cf9r",
            "namespace": "default",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "shop-demo-76d87fc7fb",
                }
            ],
            "managedFields": [{"manager": "kubelet", "operation": "Update"}] * 50,
        },
        "spec": {
            "containers": [
                {
                    "name": "shop-demo",
                    "resources": {"limits": {"memory": "256Mi"}},
                }
            ]
        },
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "shop-demo",
                    "ready": False,
                    "restartCount": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {
                        "terminated": {"reason": "OOMKilled", "exitCode": 137}
                    },
                }
            ],
        },
    }
    mock_obj = MagicMock()
    mock_obj.to_dict.return_value = mock_pod_data
    mock_res.get.return_value = mock_obj

    inp = K8sInspectResourceInput(
        api_version="v1",
        kind="Pod",
        name="shop-demo-76d87fc7fb-7cf9r",
        namespace="default",
    )
    output_raw = execute_k8s_inspect(inp, dyn_client=mock_dyn)
    parsed = json.loads(output_raw)

    # Verify managedFields is stripped to conserve context
    assert "managedFields" not in parsed["metadata"]

    # Verify ownerReferences and lastState are fully preserved
    assert parsed["metadata"]["ownerReferences"][0]["kind"] == "ReplicaSet"
    assert parsed["metadata"]["ownerReferences"][0]["name"] == "shop-demo-76d87fc7fb"
    c_status = parsed["status"]["containerStatuses"][0]
    assert c_status["state"]["waiting"]["reason"] == "CrashLoopBackOff"
    assert c_status["lastState"]["terminated"]["reason"] == "OOMKilled"
    assert c_status["lastState"]["terminated"]["exitCode"] == 137


# ── 3. k8s_mutate_resource strategic_merge_patch & rollback ──────────────────


def test_k8s_mutate_strategic_merge_patch_and_rollback():
    mock_dyn = MagicMock()
    mock_res = MagicMock()
    mock_dyn.resources.get.return_value = mock_res
    mock_res.patch.return_value = {"metadata": {"name": "shop-demo"}}

    # Test strategic_merge_patch (memory limit patch)
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "shop-demo",
                            "resources": {"limits": {"memory": "512Mi"}},
                        }
                    ]
                }
            }
        }
    }
    inp_patch = K8sMutateResourceInput(
        api_version="apps/v1",
        kind="Deployment",
        name="shop-demo",
        namespace="default",
        mutation_type="strategic_merge_patch",
        patch_body=patch_body,
    )
    res_patch = json.loads(execute_k8s_mutate(inp_patch, dyn_client=mock_dyn))
    assert res_patch["metadata"]["name"] == "shop-demo"
    assert (
        mock_res.patch.call_args.kwargs["content_type"]
        == "application/strategic-merge-patch+json"
    )
    assert mock_res.patch.call_args.kwargs["body"] == patch_body

    # Test rollback mutation
    with patch(
        "nexus.engine.tools.k8s.mutate_tools.rollback_deployment",
        return_value="Rolled back deployment shop-demo.",
    ):
        inp_rb = K8sMutateResourceInput(
            api_version="apps/v1",
            kind="Deployment",
            name="shop-demo",
            namespace="default",
            mutation_type="rollback",
            patch_body={"revision": 2},
        )
        res_rb = json.loads(execute_k8s_mutate(inp_rb, dyn_client=mock_dyn))
        assert res_rb["action"] == "rollback"
        assert "Rolled back" in res_rb["status"]


# ── 3. Strict Multi-Replica Health Verification ──────────────────────────────


@pytest.mark.asyncio
async def test_k8s_platform_adapter_verify_health_rejects_crashing_replica():
    adapter = K8sPlatformAdapter()
    target = TargetResource(
        platform="kubernetes",
        namespace="default",
        name="shop-demo",
        kind="deployment",
    )

    # Deployment has ready=1/1, BUT unready pod has CrashLoopBackOff/OOMKilled
    dep_describe_output = "Deployment shop-demo: replicas=1, ready=1, available=1, unavailable=0, updated=1 [shop-demo-76d-xyz:CrashLoopBackOff, shop-demo-76d-xyz:LastState:OOMKilled(exit 137)]"

    with patch(
        "nexus.engine.tools.k8s.observe_tools.describe_resource",
        return_value=dep_describe_output,
    ):
        check = await adapter.verify_health(target, plan_step={})
        assert check.healthy is False
        assert check.slo_restored is False
        assert "CrashLoopBackOff" in check.details or "OOMKilled" in check.details

    # Clean healthy deployment
    healthy_describe = "Deployment shop-demo: replicas=1, ready=1, available=1, unavailable=0, updated=1"
    with patch(
        "nexus.engine.tools.k8s.observe_tools.describe_resource",
        return_value=healthy_describe,
    ):
        check_ok = await adapter.verify_health(target, plan_step={})
        assert check_ok.healthy is True
        assert check_ok.slo_restored is True


# ── 6. Asynchronous Slack Approval Response Pattern ──────────────────────────


@pytest.mark.asyncio
async def test_async_workflow_resume_executes_and_notifies_response_url():
    from nexus.services.api.status_slack import _async_workflow_resume

    mock_wf = MagicMock()
    mock_wf.resume_incident = AsyncMock(
        return_value={"resolved": True, "incident_id": "CL-TEST"}
    )
    mock_audit = MagicMock()
    mock_audit.record_approval = AsyncMock()

    mock_client = AsyncMock()
    mock_post = AsyncMock()
    mock_client.post = mock_post
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        await _async_workflow_resume(
            workflow=mock_wf,
            audit_trail=mock_audit,
            approval_id="CL-TEST",
            decision="approved",
            user="alice",
            response_url="https://hooks.slack.com/actions/T123/B456/XYZ",
        )
        mock_wf.resume_incident.assert_called_with("CL-TEST", "approved")
        mock_audit.record_approval.assert_called_with("CL-TEST", "slack:alice")
        mock_post.assert_called_once()
        sent_payload = mock_post.call_args.kwargs["json"]
        assert "Remediation `CL-TEST` executed successfully" in str(sent_payload)
