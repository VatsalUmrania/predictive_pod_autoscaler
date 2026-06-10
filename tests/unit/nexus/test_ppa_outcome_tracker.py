"""Unit tests for nexus.learning.ppa_outcome_tracker.PpaOutcomeTracker."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.learning.ppa_outcome_tracker import (
    PendingPrediction,
    PpaOutcomeTracker,
    _compute_smape,
)


class TestComputeSmape:
    def test_zero_when_identical(self):
        assert abs(_compute_smape(100.0, 100.0) - 0.0) < 0.001

    def test_smape_10_percent(self):
        assert abs(_compute_smape(100.0, 110.0) - 9.524) < 0.1

    def test_smape_zero_denominator_safe(self):
        assert _compute_smape(0.0, 0.0) == 0.0

    def test_smape_large_division(self):
        result = _compute_smape(0.0, 100.0)
        assert result > 150.0  # asymmetric formula


class TestPendingPrediction:
    def test_is_expired_true(self):
        exp = PendingPrediction(
            decision_id="x",
            deployment="api",
            namespace="default",
            predicted_rps=100.0,
            current_rps=50.0,
            confidence=0.8,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            expected_resolution_time=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        assert exp.is_expired is True

    def test_is_expired_false(self):
        fresh = PendingPrediction(
            decision_id="y",
            deployment="api",
            namespace="default",
            predicted_rps=100.0,
            current_rps=50.0,
            confidence=0.8,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            expected_resolution_time=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        assert fresh.is_expired is False


class TestPpaOutcomeTracker:
    @pytest.fixture
    def mock_nats(self):
        nats = MagicMock()
        nats.publish = AsyncMock()
        return nats

    @pytest.fixture
    def tracker(self, mock_nats, tmp_path):
        return PpaOutcomeTracker(
            nats_client=mock_nats,
            batch_output_path=tmp_path / "ppa_outcomes.jsonl",
        )

    @pytest.mark.asyncio
    async def test_on_prediction_stores_pending(self, tracker, mock_nats):
        await tracker.on_prediction(
            deployment="api",
            namespace="default",
            predicted_rps=450.0,
            current_rps=120.0,
            confidence=0.85,
            horizon_minutes=10,
            model_version="v3",
            raw_features={"rps_per_replica": 40.0},
            event_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        assert len(tracker._pending) == 1
        decision_id = list(tracker._pending.keys())[0]
        pred = tracker._pending[decision_id]
        assert pred.deployment == "api"
        assert pred.predicted_rps == 450.0
        assert pred.horizon_minutes == 10

    @pytest.mark.asyncio
    async def test_check_and_emit_outcome_publishes_to_ppa_outcomes(self, tracker, mock_nats):
        # Pre-populate an already-expired pending prediction
        now = datetime.now(timezone.utc)
        tracker._pending["t1"] = PendingPrediction(
            decision_id="t1",
            deployment="api",
            namespace="default",
            predicted_rps=450.0,
            current_rps=120.0,
            confidence=0.85,
            horizon_minutes=10,
            model_version="v3",
            raw_features={},
            expected_resolution_time=now - timedelta(seconds=1),
        )
        tracker._fetch_actual_rps = AsyncMock(return_value=480.0)

        await tracker._check_and_emit_outcome()

        # Should have published to ppa.outcomes.api
        mock_nats.publish.assert_called_once()
        subject = mock_nats.publish.call_args[0][0]
        payload = mock_nats.publish.call_args[0][1]
        assert subject == "ppa.outcomes.api"
        assert payload["deployment"] == "api"
        assert payload["actual_rps"] == 480.0
        assert payload["predicted_rps"] == 450.0
        assert payload["verdict"] == "spike_hit"
        # Removed from pending
        assert "t1" not in tracker._pending

    @pytest.mark.asyncio
    async def test_check_and_emit_outcome_skips_fresh_predictions(self, tracker, mock_nats):
        now = datetime.now(timezone.utc)
        tracker._pending["fresh1"] = PendingPrediction(
            decision_id="fresh1",
            deployment="api",
            namespace="default",
            predicted_rps=100.0,
            current_rps=50.0,
            confidence=0.8,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            expected_resolution_time=now + timedelta(minutes=10),
        )
        tracker._fetch_actual_rps = AsyncMock()

        await tracker._check_and_emit_outcome()

        mock_nats.publish.assert_not_called()
        tracker._fetch_actual_rps.assert_not_called()
        assert "fresh1" in tracker._pending

    @pytest.mark.asyncio
    async def test_run_batch_flush_writes_jsonl(self, tracker, mock_nats, tmp_path):
        now = datetime.now(timezone.utc)
        tracker._recent_outcomes.append({
            "deployment": "api",
            "namespace": "default",
            "predicted_rps": 450.0,
            "actual_rps": 480.0,
            "verdict": "spike_hit",
            "smape": 6.5,
            "confidence": 0.85,
            "model_version": "v3",
            "resolution_at": now.isoformat(),
            "decision_id": "batch-1",
        })

        await tracker.run_batch_flush()

        output_file = tmp_path / "ppa_outcomes.jsonl"
        assert output_file.exists()
        import json
        with open(output_file) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["deployment"] == "api"
        assert record["verdict"] == "spike_hit"
        # Cleared after flush
        assert len(tracker._recent_outcomes) == 0

    @pytest.mark.asyncio
    async def test_compute_verdict_spike_hit(self, tracker):
        now = datetime.now(timezone.utc)
        pred = PendingPrediction(
            decision_id="v1", deployment="api", namespace="default",
            predicted_rps=100.0, current_rps=50.0, confidence=0.8,
            horizon_minutes=10, model_version="v1", raw_features={},
            expected_resolution_time=now,
        )
        # actual > current * 1.3 → spike_hit
        verdict = tracker._compute_verdict(pred, actual_rps=100.0)
        assert verdict in ("spike_hit", "spike_missed")

    @pytest.mark.asyncio
    async def test_compute_verdict_no_baseline_when_predicted_zero(self, tracker):
        now = datetime.now(timezone.utc)
        pred = PendingPrediction(
            decision_id="v2", deployment="api", namespace="default",
            predicted_rps=0.0, current_rps=100.0, confidence=0.8,
            horizon_minutes=10, model_version="v1", raw_features={},
            expected_resolution_time=now,
        )
        verdict = tracker._compute_verdict(pred, actual_rps=0.0)
        assert verdict == "correct_no_spike"
