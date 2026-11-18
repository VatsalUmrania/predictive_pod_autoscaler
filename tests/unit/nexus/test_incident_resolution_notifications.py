"""Unit tests for incident resolution and escalation notifications in Notifier and Dashboard.

Covers:
  - Notifier.notify_incident_resolved() Block Kit formatting & Slack webhook POST
  - Notifier.notify_incident_escalated() Block Kit formatting & Slack webhook POST
  - Notifier._on_incident_resolved_message() & _on_incident_escalated_message() NATS handlers
  - dashboard.record_incident_resolution() formatting & DB persistence
  - IncidentWorkflow._handle_workflow_completion() NATS publication & dashboard recording
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.integration.dashboard import record_incident_resolution
from nexus.integration.notifier import Notifier


def _policy(app_name: str, webhook: str | None) -> dict:
    return {app_name: {"notifications": {"slack_webhook": webhook}}}


@pytest.mark.asyncio
async def test_notify_incident_resolved_sends_slack_payload():
    """With a webhook configured, notify_incident_resolved builds and sends Block Kit payload."""
    notifier = Notifier()
    notifier._send = AsyncMock()

    policy = _policy("checkout-service", "https://hooks.slack.test/RESOLVED")
    with patch("nexus.integration.dashboard._policy_cache", policy):
        await notifier.notify_incident_resolved(
            app_name="checkout-service",
            incident_id="inc-12345",
            target="default/checkout-service",
            failure_class="PodCrashLoopBackOff",
            root_cause="OOM killed due to memory leak in query cache",
            action_taken="Restarted deployment and bumped memory limits",
            verification_details="Pod status Running, error rate < 0.1%, SLO restored",
            confidence=0.95,
            duration_s=12.4,
        )

    notifier._send.assert_awaited_once()
    webhook, payload, app = notifier._send.await_args.args
    assert webhook == "https://hooks.slack.test/RESOLVED"
    assert app == "checkout-service"

    attachments = payload["attachments"]
    assert len(attachments) == 1
    att = attachments[0]
    assert att["color"] == "#059669"  # Green
    assert "default/checkout-service" in att["fallback"]
    assert "PodCrashLoopBackOff" in att["fallback"]

    blocks_str = str(att["blocks"])
    assert "inc-12345" in blocks_str
    assert "default/checkout-service" in blocks_str
    assert "PodCrashLoopBackOff" in blocks_str
    assert "OOM killed due to memory leak" in blocks_str
    assert "Restarted deployment" in blocks_str
    assert "SLO restored" in blocks_str
    assert "95%" in blocks_str
    assert "12.4s" in blocks_str


@pytest.mark.asyncio
async def test_notify_incident_resolved_skips_when_no_webhook():
    """When app has no webhook configured, notify_incident_resolved silently skips."""
    notifier = Notifier()
    notifier._send = AsyncMock()

    policy = _policy("silent-app", None)
    with (
        patch("nexus.integration.dashboard._policy_cache", policy),
        patch.dict("os.environ", {}, clear=True),
    ):
        await notifier.notify_incident_resolved(
            app_name="silent-app",
            incident_id="inc-999",
            target="default/silent-app",
        )

    notifier._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_incident_escalated_sends_slack_payload():
    """When an incident escalates, notify_incident_escalated sends red alert."""
    notifier = Notifier()
    notifier._send = AsyncMock()

    policy = _policy("order-service", "https://hooks.slack.test/ESCALATE")
    with patch("nexus.integration.dashboard._policy_cache", policy):
        await notifier.notify_incident_escalated(
            app_name="order-service",
            incident_id="inc-888",
            target="default/order-service",
            reason="Pod keeps crash looping after 3 restart attempts",
            failure_class="CrashLoopBackOff",
            root_cause="Fatal panic on database connection timeout",
        )

    notifier._send.assert_awaited_once()
    webhook, payload, app = notifier._send.await_args.args
    assert webhook == "https://hooks.slack.test/ESCALATE"
    att = payload["attachments"][0]
    assert att["color"] == "#dc2626"  # Red
    assert "ESCALATED" in str(att["blocks"])
    assert "keeps crash looping" in str(att["blocks"])
    assert "Fatal panic on database" in str(att["blocks"])


@pytest.mark.asyncio
async def test_on_incident_resolved_message_maps_nats_payload():
    """NATS message handler extracts fields and delegates to notify_incident_resolved."""
    notifier = Notifier()
    notifier.notify_incident_resolved = AsyncMock()

    data = {
        "incident_id": "inc-456",
        "app": "frontend",
        "target": "default/frontend",
        "rca": {
            "failure_class": "HighErrorRate",
            "root_cause": "Nginx upstream timeout",
            "confidence": 0.88,
        },
        "plan": {
            "steps": [
                {"description": "Reloaded Nginx configuration and refreshed endpoints"}
            ],
            "failure_mode": "runbook_nginx_reload_v1",
        },
        "verification": {
            "healthy": True,
            "slo_restored": True,
            "details": "HTTP 5xx error rate dropped to 0.0%",
        },
        "duration_s": 8.5,
    }

    await notifier._on_incident_resolved_message(data)

    notifier.notify_incident_resolved.assert_awaited_once()
    kwargs = notifier.notify_incident_resolved.await_args.kwargs
    assert kwargs["app_name"] == "frontend"
    assert kwargs["incident_id"] == "inc-456"
    assert kwargs["target"] == "default/frontend"
    assert kwargs["failure_class"] == "HighErrorRate"
    assert kwargs["root_cause"] == "Nginx upstream timeout"
    assert "Reloaded Nginx" in kwargs["action_taken"]
    assert "dropped to 0.0%" in kwargs["verification_details"]
    assert kwargs["confidence"] == 0.88
    assert kwargs["duration_s"] == 8.5


@pytest.mark.asyncio
async def test_on_incident_escalated_message_maps_nats_payload():
    """NATS message handler extracts fields and delegates to notify_incident_escalated."""
    notifier = Notifier()
    notifier.notify_incident_escalated = AsyncMock()

    data = {
        "incident_id": "inc-777",
        "app": "billing",
        "target": "default/billing",
        "rca": {
            "failure_class": "DiskPressure",
            "root_cause": "Disk utilization 98% on host",
        },
        "reason": "Disk cleanup action failed: Permission denied",
    }

    await notifier._on_incident_escalated_message(data)

    notifier.notify_incident_escalated.assert_awaited_once()
    kwargs = notifier.notify_incident_escalated.await_args.kwargs
    assert kwargs["app_name"] == "billing"
    assert kwargs["incident_id"] == "inc-777"
    assert kwargs["target"] == "default/billing"
    assert kwargs["failure_class"] == "DiskPressure"
    assert "Permission denied" in kwargs["reason"]


@pytest.mark.asyncio
async def test_record_incident_resolution_persists_to_db():
    """record_incident_resolution formats row and invokes _write_incident."""
    with patch(
        "nexus.integration.dashboard._write_incident", new_callable=AsyncMock
    ) as mock_write:
        data = {
            "incident_id": "inc-db-1",
            "target": "default/payment-api",
            "app": "payment-api",
            "outcome": "success",
            "rca": {
                "failure_class": "DatabaseConnectionExhaustion",
                "root_cause": "Connection pool leaked by hung transactions",
                "confidence": 0.92,
            },
            "plan": {
                "failure_mode": "runbook_db_pool_reset_v1",
                "steps": [{"description": "Reset connection pool"}],
            },
            "resolved_at": "2026-09-11T12:00:00Z",
        }

        await record_incident_resolution(data)

        mock_write.assert_awaited_once()
        row = mock_write.await_args.args[0]
        assert row["incident_id"] == "inc-db-1"
        assert row["target"] == "default/payment-api"
        assert row["outcome"] == "success"
        assert row["level"] == 3
        assert row["confidence"] == 0.92
        assert "Reset connection pool on default/payment-api" in row["description"]
        assert "Successfully resolved and verified healthy" in row["description"]


@pytest.mark.asyncio
async def test_workflow_handle_completion_publishes_nats_and_records():
    """IncidentWorkflow._handle_workflow_completion broadcasts on NATS, persists to DB, and publishes report."""
    from nexus.bus.nats.client import NATSClient
    from nexus.graph.workflow import IncidentWorkflow

    workflow = IncidentWorkflow.__new__(IncidentWorkflow)
    workflow.nats = MagicMock()
    workflow.nats.is_connected = True
    workflow.nats.publish_raw = AsyncMock()

    result_state = {
        "incident_id": "inc-wf-1",
        "status": "RESOLVED",
        "namespace": "ecommerce",
        "resource_kind": "Deployment",
        "service_name": "cart-service",
        "target_resource": "ecommerce/Deployment/cart-service",
        "failure_class": "missing_config_key",
        "suspected_cause": "Config map missing key",
        "confidence": 0.9,
        "rca_result": {
            "failure_type": "missing_config_key",
            "root_cause_summary": "Config map missing key",
            "confidence": 0.9,
        },
        "remediation_plan": {
            "steps": [
                {
                    "step": 1,
                    "tool_name": "k8s_mutate_resource",
                    "description": "Mounted missing config map key",
                }
            ],
        },
        "execution_results": [
            {
                "step": 1,
                "tool_name": "k8s_mutate_resource",
                "status": "SUCCESS",
                "output": "Patched",
            }
        ],
        "verification_passed": True,
        "verification_feedback": "Ready 3/3",
    }

    with (
        patch(
            "nexus.integration.dashboard.record_incident_resolution",
            new_callable=AsyncMock,
        ) as mock_rec,
        patch.object(NATSClient, "_instance", workflow.nats),
        patch(
            "nexus.integration.incident_report._store_report", new_callable=AsyncMock
        ),
    ):
        await workflow._handle_workflow_completion(
            result=result_state,
            inc_id="inc-wf-1",
            thread_id="thread-wf-1",
        )

        # 1. Check NATS publications: lifecycle event, then the post-incident report
        calls = workflow.nats.publish_raw.await_args_list
        assert [c.args[0] for c in calls] == [
            "nexus.lifecycle.resolved",
            "nexus.lifecycle.report_ready",
        ]
        payload = calls[0].args[1]
        assert payload["incident_id"] == "inc-wf-1"
        assert payload["target"] == "ecommerce/Deployment/cart-service"
        assert payload["outcome"] == "success"

        # 2. Check dashboard DB recording
        mock_rec.assert_awaited_once()
        db_payload = mock_rec.await_args.args[0]
        assert db_payload["incident_id"] == "inc-wf-1"
        assert db_payload["target"] == "ecommerce/Deployment/cart-service"

        # 3. Report-ready payload carries the report URL + summary
        report_msg = calls[1].args[1]
        assert report_msg["incident_id"] == "inc-wf-1"
        assert report_msg["report_url"].endswith("/developer/incidents/inc-wf-1/report")
        assert report_msg["summary"]  # template fallback still produces a summary


@pytest.mark.asyncio
async def test_escalation_flow_maps_new_rca_schema_and_avoids_duplicate_slack():
    """Escalated workflow publishes RCA + proposed fix without Unknown/ESCALATED placeholders or duplicate Slack sends."""
    from nexus.bus.nats.client import NATSClient
    from nexus.graph.nodes.escalate import derive_escalation_reason, escalate_node
    from nexus.graph.workflow_completion import handle_workflow_completion

    state = {
        "incident_id": "CL-46C15FD3",
        "platform": "k8s",
        "namespace": "default",
        "resource_kind": "Deployment",
        "service_name": "shop-demo",
        "target_resource": "default/Deployment/shop-demo",
        "failure_class": "readiness_probe_startup_race_condition",
        "suspected_cause": "Container took 10s to bind port 8001 while readinessProbe initialDelaySeconds is 2s.",
        "confidence": 0.91,
        "rca_result": {
            "failure_type": "readiness_probe_startup_race_condition",
            "root_cause_summary": "Container took 10s to bind port 8001 while readinessProbe initialDelaySeconds is 2s.",
            "confidence": 0.91,
            "evidence": ["connect: connection refused on :8001", "initialDelaySeconds: 2"],
            "can_auto_remediate": False,
            "recommended_strategy": "Increase readinessProbe.initialDelaySeconds from 2s to 15s on Deployment/shop-demo.",
        },
        "remediation_plan": {},
        "execution_results": [],
        "status": "DIAGNOSED",
    }

    reason = derive_escalation_reason(state)
    assert "can_auto_remediate=False" in reason
    assert reason != "ESCALATED"

    mock_nats = MagicMock()
    mock_nats.is_connected = True
    mock_nats.publish_raw = AsyncMock()

    with (
        patch.object(NATSClient, "_instance", mock_nats),
        patch(
            "nexus.graph.nodes.escalate.sync_discovered_target_identity",
            new_callable=AsyncMock,
        ),
        patch.object(Notifier, "notify_incident_escalated", new_callable=AsyncMock) as mock_direct,
    ):
        esc_update = await escalate_node(state)
        assert esc_update["status"] == "ESCALATED"
        assert "can_auto_remediate=False" in esc_update["error"]
        # When NATS is connected, escalate_node must NOT send a duplicate direct Slack message
        mock_direct.assert_not_awaited()

    final_state = {**state, **esc_update}
    with patch(
        "nexus.integration.dashboard.record_incident_resolution",
        new_callable=AsyncMock,
    ):
        await handle_workflow_completion(
            result=final_state,
            inc_id="CL-46C15FD3",
            thread_id="CL-46C15FD3",
            nats=mock_nats,
            publish_report=False,
        )

    mock_nats.publish_raw.assert_awaited_once()
    subject, published = mock_nats.publish_raw.await_args.args
    assert subject == "nexus.lifecycle.escalated"
    assert published["failure_class"] == "readiness_probe_startup_race_condition"
    assert "initialDelaySeconds" in published["recommended_strategy"]
    assert published["reason"] != "ESCALATED"

    # Now feed that exact NATS payload into Notifier and verify the single Slack card has RCA + Proposed Fix
    notifier = Notifier()
    notifier._send = AsyncMock()
    with patch(
        "nexus.integration.dashboard._policy_cache",
        _policy("default", "https://hooks.slack.test/ESC"),
    ):
        await notifier._on_incident_escalated_message(published)

    notifier._send.assert_awaited_once()
    _, slack_payload, _ = notifier._send.await_args.args
    blocks_str = str(slack_payload["attachments"][0]["blocks"])
    assert "readiness_probe_startup_race_condition" in blocks_str
    assert "Root Cause Analysis (RCA)" in blocks_str
    assert "Container took 10s to bind port 8001" in blocks_str
    assert "Proposed Fix / Recommended Strategy" in blocks_str
    assert "Increase readinessProbe.initialDelaySeconds from 2s to 15s" in blocks_str
    assert "Observed Evidence" in blocks_str
    assert "91%" in blocks_str
    assert "`Unknown`" not in blocks_str
