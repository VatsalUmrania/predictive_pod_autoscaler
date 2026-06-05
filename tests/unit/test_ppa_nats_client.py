"""Unit tests for ppa.bus.nats_client.PpaNATSClient."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ppa.bus.nats_client import PpaNATSClient


class TestPpaNATSClientConstruction:
    def test_default_url(self):
        client = PpaNATSClient()
        assert client._url == "nats://localhost:4222"

    def test_custom_url(self):
        client = PpaNATSClient(nats_url="nats://custom:5222")
        assert client._url == "nats://custom:5222"

    def test_not_connected_by_default(self):
        client = PpaNATSClient()
        assert client.is_connected is False


class TestPpaNATSClientPublish:
    @pytest.mark.asyncio
    async def test_publish_sends_raw_bytes(self):
        client = PpaNATSClient()
        mock_js = AsyncMock()
        mock_ack = MagicMock()
        mock_ack.seq = 42
        mock_js.publish.return_value = mock_ack
        client._js = mock_js

        await client.publish(
            subject="ppa.predictions.payments-api",
            payload={"deployment": "payments-api", "predicted_rps": 450.0},
        )

        mock_js.publish.assert_called_once()
        call_args = mock_js.publish.call_args
        assert call_args[0][0] == "ppa.predictions.payments-api"
        assert b'"deployment"' in call_args[0][1]
        assert call_args[1] == {"stream": "PPA_PREDICTIONS"}

    @pytest.mark.asyncio
    async def test_publish_raises_when_not_connected(self):
        client = PpaNATSClient()
        with pytest.raises(RuntimeError, match="not connected"):
            await client.publish("ppa.predictions.api", {"rps": 50})

    @pytest.mark.asyncio
    async def test_publish_raises_on_nats_failure(self):
        client = PpaNATSClient()
        mock_js = AsyncMock()
        mock_js.publish.side_effect = Exception("NATS down")
        client._js = mock_js

        with pytest.raises(Exception):  # noqa: B017
            await client.publish("ppa.predictions.api", {"rps": 50})
