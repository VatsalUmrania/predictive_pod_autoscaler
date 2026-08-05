"""Unit tests for the L3 approval-requirement notification path in Notifier.

Covers the new endpoint of the wiring added in the self-healing plan:
  HumanApprovalQueue.enqueue → publish_raw("nexus.approvals.required") →
  Notifier._on_approval_message → notify_approval_required() → Slack webhook.

We test the Notifier half directly (the queue→publish and publish_raw halves
are covered by test_human_approval_queue.py and test_nats_client_publish_raw.py),
driving both notify_approval_required() and the NATS message handler.
"""

from unittest.mock import AsyncMock, patch

import pytest

from nexus.integration.notifier import Notifier


def _policy(app_name: str, webhook: str | None) -> dict:
    return {app_name: {"notifications": {"slack_webhook": webhook}}}


@pytest.mark.asyncio
async def test_notify_approval_required_sends_slack_payload():
    """With a webhook configured, notify_approval_required builds & sends the payload."""
    notifier = Notifier()
    notifier._send = AsyncMock()

    policy = _policy("payments", "https://hooks.slack.test/ABC")
    with patch("nexus.integration.dashboard._policy_cache", policy):
        await notifier.notify_approval_required(
            app_name="payments",
            approval_id="ABCD1234",
            runbook_id="runbook_lambda_error_spike_v1",
            target="default/auth-lambda",
            healing_level=3,
            confidence=0.62,
        )

    notifier._send.assert_awaited_once()
    webhook, payload, app = notifier._send.await_args.args
    assert webhook == "https://hooks.slack.test/ABC"
    assert app == "payments"
    # Payload is a Slack attachments structure carrying our L3 context.
    assert "attachments" in payload
    fallback = payload["attachments"][0]["fallback"]
    assert "runbook_lambda_error_spike_v1" in fallback
    assert "default/auth-lambda" in fallback
    # The approve-command instruction must reach the reader.
    blob = str(payload)
    assert "nexus approve ABCD1234" in blob
    assert "L3" in blob and "Approval" in blob


@pytest.mark.asyncio
async def test_notify_approval_required_skips_when_no_webhook():
    """No webhook for the app → notify_approval_required sends nothing."""
    notifier = Notifier()
    notifier._send = AsyncMock()

    with patch("nexus.integration.dashboard._policy_cache", _policy("payments", None)):
        await notifier.notify_approval_required(
            app_name="payments",
            approval_id="ABCD1234",
            runbook_id="runbook_x",
            target="t",
            healing_level=3,
            confidence=0.6,
        )

    notifier._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_approval_message_maps_nats_payload_and_notifies():
    """The NATS handler maps the raw dict fields into notify_approval_required."""
    notifier = Notifier()
    notifier.notify_approval_required = AsyncMock()  # type: ignore[method-assign]

    data = {
        "app": "orders",
        "approval_id": "DEAD0001",
        "runbook_id": "runbook_dynamo_throttle_v1",
        "target": "default/orders",
        "healing_level": 3,
        "confidence": 0.55,
        # extra fields the queue sends but the handler ignores:
        "action_type": "rollback_alias",
        "incident_id": "INC-7",
    }

    await notifier._on_approval_message(data)

    notifier.notify_approval_required.assert_awaited_once()
    kwargs = notifier.notify_approval_required.await_args.kwargs
    assert kwargs["app_name"] == "orders"
    assert kwargs["approval_id"] == "DEAD0001"
    assert kwargs["runbook_id"] == "runbook_dynamo_throttle_v1"
    assert kwargs["target"] == "default/orders"
    assert kwargs["healing_level"] == 3
    assert kwargs["confidence"] == 0.55


@pytest.mark.asyncio
async def test_on_approval_message_swallows_errors():
    """A handler failure must not propagate out of _on_approval_message."""
    notifier = Notifier()
    notifier.notify_approval_required = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("boom")
    )
    # Must not raise.
    await notifier._on_approval_message({"app": "x"})

    # Sanity: it attempted exactly once.
    notifier.notify_approval_required.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_approval_message_defaults_missing_healing_level():
    """Missing healing_level in the payload defaults to L3 (human-approval band)."""
    notifier = Notifier()
    notifier.notify_approval_required = AsyncMock()  # type: ignore[method-assign]

    await notifier._on_approval_message(
        {"app": "svc", "approval_id": "A1", "runbook_id": "rb", "target": "t"}
    )

    kwargs = notifier.notify_approval_required.await_args.kwargs
    assert kwargs["healing_level"] == 3
    assert kwargs["confidence"] == 0.0
