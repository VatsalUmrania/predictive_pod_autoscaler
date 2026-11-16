"""Tests for Slack Block Kit formatting showing YAML manifests and diagnostic logs."""

from nexus.integration.slack_action_templates import build_approval_required_payload
from nexus.integration.slack_agentic_templates import (
    build_agentic_approval_payload,
    build_slack_approval_blocks,
)


def test_build_approval_required_payload_has_yaml_and_logs():
    """Verify approval payload includes YAML manifest and diagnostic logs, without Runbook header."""
    context = {
        "remediation_yaml": "apiVersion: apps/v1\nkind: Deployment\nspec:\n  replicas: 4",
        "diagnostic_logs": "Container crashloop exit 137\nOOMKilled: true",
        "rca": {"failure_class": "resource_exhaustion", "root_cause": "OOMKilled pod"},
        "event": {"signal_type": "pod_crashloop", "severity": "critical", "namespace": "prod"},
    }

    payload = build_approval_required_payload(
        app_name="shop-demo",
        approval_id="APP-1234",
        runbook_id="k8s_restart_pod",
        target="prod/shop-demo",
        healing_level=1,
        confidence=0.92,
        context=context,
    )

    blocks = payload["attachments"][0]["blocks"]
    block_texts = []
    for b in blocks:
        if "text" in b and isinstance(b["text"], dict):
            block_texts.append(b["text"].get("text", ""))
        for f in b.get("fields", []):
            block_texts.append(f.get("text", ""))

    all_text = " ".join(block_texts)

    # Must contain YAML and diagnostic logs
    assert "Proposed Remediation Manifest (YAML)" in all_text
    assert "replicas: 4" in all_text
    assert "Diagnostic Evidence & Pod Logs" in all_text
    assert "OOMKilled" in all_text

    # Must NOT have Runbook header
    assert "*Runbook:*" not in all_text


def test_build_agentic_approval_payload_has_yaml():
    """Verify agentic approval payload displays proposed steps as YAML."""
    payload = build_agentic_approval_payload(
        incident_id="INC-4321",
        target_service="checkout-service",
        causal_rationale="Memory limit saturated",
        proposed_steps=[
            {
                "tool": "kubernetes_scale_deployment",
                "arguments": {"namespace": "default", "deployment": "checkout-service", "replicas": 5},
            }
        ],
        confidence=0.95,
    )

    blocks = payload["attachments"][0]["blocks"]
    all_text = " ".join(
        b.get("text", {}).get("text", "")
        for b in blocks
        if "text" in b and isinstance(b["text"], dict)
    )

    assert "Proposed Remediation Manifest (YAML)" in all_text
    assert "replicas: 5" in all_text
