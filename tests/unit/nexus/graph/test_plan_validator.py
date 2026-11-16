"""
Unit Tests for Plane 3: Pre-Flight Plan Validation & Optimistic Concurrency Control (OCC)
========================================================================================
Verifies:
  1. Server-side dry-run validation (dry_run='All') using DynamicClient.
  2. Mathematical inverse rollback computation via jsonpatch.
  3. AST mutated paths extraction for downstream OPA evaluation.
  4. Live ResourceVersion capture for Optimistic Concurrency Control (OCC).
  5. Canonical SHA-256 plan hash generation.
  6. LangGraph plan_validator_node state transitions on success and failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.db.models import IncidentStatus
from nexus.graph.nodes.plan_validator import (
    plan_validator_node,
    validate_and_seal_plan,
)


class MockResourceObject:
    def __init__(self, data: dict):
        self._data = data
        self.metadata = MagicMock()
        self.metadata.resourceVersion = data.get("metadata", {}).get(
            "resourceVersion", "100"
        )

    def to_dict(self):
        return self._data


def test_validate_and_seal_plan_success():
    """Verify server-side dry-run extracts version, computes inverse patch and AST paths."""
    live_state = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout-svc",
            "namespace": "production",
            "resourceVersion": "54321",
        },
        "spec": {
            "replicas": 2,
            "template": {
                "spec": {"containers": [{"name": "app", "image": "checkout:v1.0.0"}]}
            },
        },
    }

    patched_state = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkout-svc",
            "namespace": "production",
            "resourceVersion": "54321",
        },
        "spec": {
            "replicas": 4,
            "template": {
                "spec": {"containers": [{"name": "app", "image": "checkout:v1.0.0"}]}
            },
        },
    }

    mock_client = MagicMock()
    mock_resource = MagicMock()
    mock_client.resources.get.return_value = mock_resource

    # 1. get() returns current live object
    mock_resource.get.return_value = MockResourceObject(live_state)
    # 2. patch(dry_run="All") returns preview state
    mock_resource.patch.return_value = MockResourceObject(patched_state)

    plan_action = {
        "name": "checkout-svc",
        "namespace": "production",
        "kind": "Deployment",
        "api_version": "apps/v1",
        "patch_payload": {"spec": {"replicas": 4}},
    }

    sealed = validate_and_seal_plan(mock_client, plan_action)

    # Verify dry_run was used
    mock_resource.patch.assert_called_once_with(
        name="checkout-svc",
        namespace="production",
        body={"spec": {"replicas": 4}},
        dry_run="All",
    )

    # Verify OCC version locked
    assert sealed["required_resource_version"] == "54321"

    # Verify AST paths extracted
    assert "/spec/replicas" in sealed["mutated_paths"]

    # Verify inverse rollback patch restores replicas from 4 back to 2
    inv_patch = sealed["rollback_json_patch"]
    assert any(
        op.get("path") == "/spec/replicas" and op.get("value") == 2 for op in inv_patch
    )

    # Verify canonical plan hash
    assert len(sealed["plan_hash"]) == 64


@pytest.mark.asyncio
async def test_plan_validator_node_happy_path():
    """Verify plan_validator_node seals actions and sets VALIDATING status."""
    live_state = {
        "metadata": {"resourceVersion": "999"},
        "spec": {"replicas": 1},
    }
    patched_state = {
        "metadata": {"resourceVersion": "999"},
        "spec": {"replicas": 3},
    }

    mock_client = MagicMock()
    mock_res = MagicMock()
    mock_client.resources.get.return_value = mock_res
    mock_res.get.return_value = MockResourceObject(live_state)
    mock_res.patch.return_value = MockResourceObject(patched_state)

    state = {
        "incident_id": "inc-val-01",
        "platform": "kubernetes",
        "plan": {
            "actions": [
                {
                    "name": "order-processor",
                    "namespace": "default",
                    "kind": "Deployment",
                    "patch_payload": {"spec": {"replicas": 3}},
                }
            ]
        },
    }

    result = await plan_validator_node(state, dyn_client=mock_client)

    assert result["validation_passed"] is True
    assert result["fsm_state"] == IncidentStatus.VALIDATING.value

    plan = result["plan"]
    assert plan["plan_hash"] is not None
    assert plan["required_resource_version"] == "999"
    assert "/spec/replicas" in plan["mutated_paths"]
    assert plan["actions"][0]["required_resource_version"] == "999"


@pytest.mark.asyncio
async def test_plan_validator_node_dry_run_failure():
    """Verify plan_validator_node catches dry-run errors and sets VALIDATION_FAILED status."""
    mock_client = MagicMock()
    mock_res = MagicMock()
    mock_client.resources.get.return_value = mock_res
    mock_res.get.return_value = MockResourceObject(
        {"metadata": {"resourceVersion": "1"}}
    )
    mock_res.patch.side_effect = RuntimeError(
        "Invalid value for spec.replicas: string instead of integer"
    )

    state = {
        "incident_id": "inc-val-fail",
        "platform": "kubernetes",
        "plan": {
            "actions": [
                {
                    "name": "order-processor",
                    "namespace": "default",
                    "kind": "Deployment",
                    "patch_payload": {"spec": {"replicas": "invalid"}},
                }
            ]
        },
    }

    result = await plan_validator_node(state, dyn_client=mock_client)

    assert result["validation_passed"] is False
    assert result["fsm_state"] == IncidentStatus.VALIDATION_FAILED.value
    assert len(result["validation_errors"]) == 1
    assert "Invalid value for spec.replicas" in result["validation_errors"][0]
