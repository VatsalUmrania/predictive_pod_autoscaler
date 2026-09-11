"""Unit tests for nexus.bus.nats_client.NATSClient.publish_raw.

``publish_raw(subject, payload)`` is the new counterpart to ``subscribe_raw``:
it lets the governance plane emit non-IncidentEvent dicts (e.g. the
``nexus.approvals.required`` event) onto the JetStream-bound ``nexus.>`` stream.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bus.nats_client import NEXUS_STREAM, NATSClient


@pytest.mark.asyncio
async def test_publish_raw_sends_json_bytes_to_subject():
    """publish_raw JSON-encodes the payload and targets the NEXUS stream."""
    client = NATSClient(nats_url="nats://localhost:4222")
    mock_js = AsyncMock()
    mock_ack = MagicMock()
    mock_ack.seq = 7
    mock_js.publish.return_value = mock_ack
    client._js = mock_js  # simulate a connected JetStream context

    await client.publish_raw(
        "nexus.approvals.required",
        {"approval_id": "ABCD1234", "runbook_id": "runbook_x", "app": "payments"},
    )

    mock_js.publish.assert_awaited_once()
    subject, data = mock_js.publish.await_args.args
    kwargs = mock_js.publish.await_args.kwargs
    assert subject == "nexus.approvals.required"
    assert kwargs == {"stream": NEXUS_STREAM}
    # Payload must be JSON-encoded bytes (matches subscribe_raw's json.loads decode)
    assert isinstance(data, (bytes, bytearray))
    import json

    decoded = json.loads(data.decode("utf-8"))
    assert decoded["approval_id"] == "ABCD1234"
    assert decoded["app"] == "payments"


@pytest.mark.asyncio
async def test_publish_raw_raises_when_not_connected():
    """No JetStream context → publish_raw rejects (caller raises — not silent)."""
    client = NATSClient()
    with pytest.raises(RuntimeError, match="not connected"):
        await client.publish_raw("nexus.approvals.required", {"x": 1})


@pytest.mark.asyncio
async def test_publish_raw_propagates_nats_failure():
    """A broker failure surfaces to the caller rather than being swallowed."""
    client = NATSClient()
    mock_js = AsyncMock()
    mock_js.publish.side_effect = Exception("NATS down")
    client._js = mock_js

    with pytest.raises(Exception, match="NATS down"):  # noqa: B017
        await client.publish_raw("nexus.approvals.required", {"x": 1})


@pytest.mark.asyncio
async def test_subscribe_drops_unparseable_incident_events():
    """Non-IncidentEvent or corrupted messages on the subscription stream are termed/acked, not looped."""
    import asyncio

    client = NATSClient()
    mock_js = AsyncMock()

    # Fake message stream with one unparseable message
    mock_msg = MagicMock()
    mock_msg.subject = "nexus.incidents.bad"
    mock_msg.data = b'{"not_an_event": true}'  # Missing required IncidentEvent fields
    mock_msg.term = AsyncMock()
    mock_msg.ack = AsyncMock()
    mock_msg.nak = AsyncMock()

    class _FakeSub:
        def __init__(self):
            self.messages = self._gen()

        async def _gen(self):
            yield mock_msg

        async def unsubscribe(self):
            pass

    mock_js.subscribe.return_value = _FakeSub()
    client._js = mock_js

    handler = AsyncMock()
    await client.subscribe(handler=handler, agent_filter=">")

    # Let the background task run
    await asyncio.sleep(0.05)

    # Handler should NOT be called on invalid message
    handler.assert_not_awaited()
    # Message should be termed or acked to avoid infinite nak loops
    assert mock_msg.term.await_count == 1 or mock_msg.ack.await_count == 1
    mock_msg.nak.assert_not_awaited()

