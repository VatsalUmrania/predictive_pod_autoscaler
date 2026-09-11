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

from nexus.integration.notifier import Notifier
from nexus.integration.dashboard import record_incident_resolution


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
    with patch("nexus.integration.dashboard._policy_cache", policy), patch.dict("os.environ", {}, clear=True):
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
            "steps": [{"description": "Reloaded Nginx configuration and refreshed endpoints"}],
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
    with patch("nexus.integration.dashboard._write_incident", new_callable=AsyncMock) as mock_write:
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
    """IncidentWorkflow._handle_workflow_completion broadcasts on NATS and persists to DB."""
    from nexus.graph.workflow import IncidentWorkflow

    workflow = IncidentWorkflow.__new__(IncidentWorkflow)
    workflow.nats = MagicMock()
    workflow.nats.publish_raw = AsyncMock()

    result_state = {
        "resolved": True,
        "fsm_state": "resolved",
        "target": {"name": "cart-service", "namespace": "ecommerce"},
        "diagnosis": {
            "failure_class": "CrashLoopBackOff",
            "root_cause": "Config map missing key",
            "confidence": 0.9,
        },
        "plan": {
            "steps": [{"description": "Mounted missing config map key"}],
        },
        "verification": {
            "healthy": True,
            "slo_restored": True,
            "details": "Ready 3/3",
        },
    }

    with patch("nexus.integration.dashboard.record_incident_resolution", new_callable=AsyncMock) as mock_rec:
        await workflow._handle_workflow_completion(
            result=result_state,
            inc_id="inc-wf-1",
            thread_id="thread-wf-1",
        )

        # 1. Check NATS publication
        workflow.nats.publish_raw.assert_awaited_once()
        subject, payload = workflow.nats.publish_raw.await_args.args
        assert subject == "nexus.lifecycle.resolved"
        assert payload["incident_id"] == "inc-wf-1"
        assert payload["target"] == "ecommerce/cart-service"
        assert payload["outcome"] == "success"

        # 2. Check dashboard DB recording
        mock_rec.assert_awaited_once()
        db_payload = mock_rec.await_args.args[0]
        assert db_payload["incident_id"] == "inc-wf-1"
        assert db_payload["target"] == "ecommerce/cart-service"
