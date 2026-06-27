"""Unit tests for Prescaler PPA prediction subscription."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bus.incident_event import SignalType
from nexus.predictive.prescaler import Prescaler


def make_ppa_payload(
    deployment="payments-api",
    namespace="production",
    predicted_rps=450.0,
    current_rps=120.0,
    confidence=0.85,
    horizon_minutes=10,
    model_version="v3.2.1",
    raw_features=None,
):
    if raw_features is None:
        raw_features = {"rps_per_replica": 40.0, "cpu_utilization_pct": 72.0}
    return {
        "deployment": deployment,
        "namespace": namespace,
        "predicted_rps": predicted_rps,
        "current_rps": current_rps,
        "confidence": confidence,
        "horizon_minutes": horizon_minutes,
        "model_version": model_version,
        "raw_features": raw_features,
    }


class TestSubscribeToPpaPredictions:
    async def _make_scaler(self) -> tuple[Prescaler, dict]:
        """Build a Prescaler with a spy on subscribe_raw."""
        stored: dict = {}

        async def spy_subscribe_raw(
            subject_pattern,
            *,
            handler,
            durable_name=None,
            stream_name=None,
        ):
            stored.update(
                {
                    "subject_pattern": subject_pattern,
                    "handler": handler,
                    "durable_name": durable_name,
                    "stream_name": stream_name,
                }
            )

        mock_nats = MagicMock()
        mock_nats.subscribe_raw = spy_subscribe_raw
        mock_ladder = MagicMock()
        scaler = Prescaler(nats_client=mock_nats, action_ladder=mock_ladder)
        return scaler, stored

    @pytest.mark.asyncio
    async def test_subscribes_with_correct_pattern_and_durable(self):
        scaler, stored = await self._make_scaler()
        await scaler.subscribe_to_ppa_predictions()
        assert stored["subject_pattern"] == "ppa.predictions.>"
        # Ephemeral consumer: no durable_name (rolling deploys must NOT collide
        # on the JetStream exclusive push-consumer lock). stream_name pins the
        # subscription to the PPA_PREDICTIONS JetStream stream.
        assert stored["durable_name"] is None
        assert stored["stream_name"] == "PPA_PREDICTIONS"
        assert callable(stored["handler"])

    @pytest.mark.asyncio
    async def test_handler_routes_event_to_handle_spike_prediction(self):
        scaler, stored = await self._make_scaler()
        scaler.handle_spike_prediction = AsyncMock()
        await scaler.subscribe_to_ppa_predictions()

        handler = stored["handler"]
        payload = make_ppa_payload()
        await handler(payload, "ppa.predictions.payments-api")

        scaler.handle_spike_prediction.assert_awaited_once()
        routed = scaler.handle_spike_prediction.call_args[0][0]
        assert routed.signal_type == SignalType.TRAFFIC_SPIKE_PREDICTED
        assert routed.context["predicted_rps"] == 450.0
        assert routed.context["current_rps"] == 120.0
        assert routed.context["raw_features"]["rps_per_replica"] == 40.0
        assert routed.context["ppa_model_version"] == "v3.2.1"
        assert routed.namespace == "production"
        assert routed.resource_name == "payments-api"
        assert routed.confidence == 0.85

    @pytest.mark.asyncio
    async def test_handler_extracts_deployment_from_subject_when_payload_lacks_it(self):
        scaler, stored = await self._make_scaler()
        scaler.handle_spike_prediction = AsyncMock()
        await scaler.subscribe_to_ppa_predictions()

        handler = stored["handler"]
        payload = {
            "namespace": "default",
            "predicted_rps": 200.0,
            "current_rps": 50.0,
            "confidence": 0.8,
        }
        await handler(payload, "ppa.predictions.api-gateway")

        routed = scaler.handle_spike_prediction.call_args[0][0]
        assert routed.resource_name == "api-gateway"
        assert routed.namespace == "default"

    @pytest.mark.asyncio
    async def test_handler_skips_non_ppa_predictions_subject(self):
        scaler, stored = await self._make_scaler()
        scaler.handle_spike_prediction = AsyncMock()
        await scaler.subscribe_to_ppa_predictions()

        handler = stored["handler"]
        payload = make_ppa_payload()
        await handler(payload, "nexus.incidents.orchestrator.traffic_spike_predicted")

        scaler.handle_spike_prediction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handler_continues_after_handle_spike_prediction_error(self):
        scaler, stored = await self._make_scaler()

        async def boom(*args, **kwargs):
            raise RuntimeError("boom from test")

        scaler.handle_spike_prediction = boom
        await scaler.subscribe_to_ppa_predictions()

        handler = stored["handler"]
        payload = make_ppa_payload()
        # Should not raise — exception is caught and logged
        await handler(payload, "ppa.predictions.payments-api")
