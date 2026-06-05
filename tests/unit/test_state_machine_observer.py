"""Unit tests for ScalerStateMachine observer-mode prediction publishing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ppa.bus.prediction_event import PpaPredictionEvent


@pytest.mark.asyncio
async def test_publish_prediction_calls_nats_with_correct_subject():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    state = CRState(predictor=None)
    state.last_prediction = 450.0
    state.active_model_version = "v3.2.1"

    config = {
        "target": "payments-api",
        "target_ns": "production",
        "target_horizon": 10,
    }

    mock_nats = AsyncMock()
    mock_nats.publish = AsyncMock()
    mock_nats.is_connected = True

    sm = ScalerStateMachine(
        cr_name="test-cr",
        cr_namespace="production",
        state=state,
        config=config,
        patch=MagicMock(),
        status={},
    )
    sm._nats = mock_nats

    await sm._publish_prediction(
        predicted_rps=450.0,
        features={"rps_per_replica": 100.0, "cpu_utilization_pct": 70.0},
    )

    mock_nats.publish.assert_called_once()
    call_args = mock_nats.publish.call_args
    subject = call_args[0][0]
    payload = call_args[0][1]
    assert subject == "ppa.predictions.payments-api"
    assert payload["deployment"] == "payments-api"
    assert payload["predicted_rps"] == 450.0
    assert payload["current_rps"] == 100.0
    assert payload["horizon_minutes"] == 10


@pytest.mark.asyncio
async def test_publish_prediction_accumulates_on_failure():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    state = CRState(predictor=None)
    state.last_prediction = 300.0
    state.active_model_version = "v1"

    config = {
        "target": "api",
        "target_ns": "default",
        "target_horizon": 10,
    }

    mock_nats = AsyncMock()
    mock_nats.publish = AsyncMock(side_effect=Exception("NATS down"))
    mock_nats.is_connected = True

    sm = ScalerStateMachine(
        cr_name="test-cr",
        cr_namespace="default",
        state=state,
        config=config,
        patch=MagicMock(),
        status={},
    )
    sm._nats = mock_nats

    await sm._publish_prediction(300.0, {"rps_per_replica": 50.0})

    assert len(sm._pending_events) == 1
    evt = sm._pending_events[0]
    assert evt.deployment == "api"
    assert evt.predicted_rps == 300.0


@pytest.mark.asyncio
async def test_publish_prediction_flushes_pending_on_success():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    state = CRState(predictor=None)
    state.last_prediction = 200.0
    state.active_model_version = "v1"

    config = {
        "target": "api",
        "target_ns": "default",
        "target_horizon": 10,
    }

    sm = ScalerStateMachine(
        cr_name="test-cr",
        cr_namespace="default",
        state=state,
        config=config,
        patch=MagicMock(),
        status={},
    )

    sm._pending_events.append(
        PpaPredictionEvent(
            deployment="api",
            namespace="default",
            predicted_rps=150.0,
            current_rps=50.0,
            confidence=0.8,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            timestamp="2026-06-05T12:00:00Z",
        )
    )

    mock_nats = AsyncMock()
    mock_nats.publish = AsyncMock()
    mock_nats.is_connected = True
    sm._nats = mock_nats

    await sm._publish_prediction(200.0, {"rps_per_replica": 50.0})

    assert mock_nats.publish.call_count == 2
    assert len(sm._pending_events) == 0


@pytest.mark.asyncio
async def test_compute_confidence_returns_base_when_no_predictor():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    state = CRState(predictor=None)
    config = {"target": "x", "target_ns": "default", "target_horizon": 10}

    sm = ScalerStateMachine(
        cr_name="t", cr_namespace="default",
        state=state, config=config,
        patch=MagicMock(), status={},
    )

    assert sm._compute_confidence() == 0.5


@pytest.mark.asyncio
async def test_compute_confidence_returns_075_for_healthy_predictor():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    mock_pred = MagicMock()
    mock_pred.check_concept_drift.return_value = {"checked": True, "detected": False}

    state = CRState(predictor=mock_pred)
    config = {"target": "x", "target_ns": "default", "target_horizon": 10}

    sm = ScalerStateMachine(
        cr_name="t", cr_namespace="default",
        state=state, config=config,
        patch=MagicMock(), status={},
    )

    assert sm._compute_confidence() == 0.75


@pytest.mark.asyncio
async def test_compute_confidence_drops_with_drift():
    from ppa.domain import CRState
    from ppa.operator.state_machine import ScalerStateMachine

    mock_pred = MagicMock()
    mock_pred.check_concept_drift.return_value = {
        "checked": True, "detected": True, "severity": "medium"
    }

    state = CRState(predictor=mock_pred)
    config = {"target": "x", "target_ns": "default", "target_horizon": 10}

    sm = ScalerStateMachine(
        cr_name="t", cr_namespace="default",
        state=state, config=config,
        patch=MagicMock(), status={},
    )

    assert sm._compute_confidence() == 0.4
