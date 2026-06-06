"""
Integration tests for the PPA → NEXUS predictive plane prediction flow.

These tests exercise cross-component wiring with an in-process mock NATS bus.
No real Kubernetes or NATS server required.

Test scope (not covered by unit tests):
    - Shared MockNATS bus: all components receive events on the same NATS
    - Durable subscription name: verified at the integration level
    - FeedbackLoop wiring: ppa_outcome_tracker.run_batch_flush() called per cycle
    - End-to-end prediction → decision → outcome → batch flush sequence

Component units (covered by unit tests):
    - tests/unit/nexus/test_prescaler_ppa_subscription.py
    - tests/unit/nexus/test_ppa_outcome_tracker.py
    - tests/unit/nexus/test_ppa_nexus_integration.py
"""

from __future__ import annotations

import json as _json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Ensure src/ is on path for package imports
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from nexus.bus.incident_event import IncidentEvent
from nexus.learning.feedback_loop import FeedbackLoop
from nexus.learning.ppa_outcome_tracker import PendingPrediction, PpaOutcomeTracker
from nexus.predictive.prescaler import PrescaleMode, Prescaler


# ── Test fixtures ──────────────────────────────────────────────────────────────

def make_ppa_payload(
    deployment: str = "payments-api",
    namespace: str = "production",
    predicted_rps: float = 450.0,
    current_rps: float = 120.0,
    confidence: float = 0.85,
    horizon_minutes: int = 10,
    model_version: str = "v3.2.1",
    raw_features: dict[str, float] | None = None,
) -> dict:
    """Build a PPA prediction payload matching PpaPredictionEvent.to_dict()."""
    return {
        "deployment": deployment,
        "namespace": namespace,
        "predicted_rps": predicted_rps,
        "current_rps": current_rps,
        "confidence": confidence,
        "horizon_minutes": horizon_minutes,
        "model_version": model_version,
        "raw_features": raw_features or {"rps_per_replica": 40.0, "cpu_utilization_pct": 72.0},
    }


class MockNATS:
    """
    In-process mock NATS that supports both publisher interfaces:

    - IncidentEvent path:  publish(event)                    → from Prescaler advisory
    - Tracker path:        publish(subject_str, dict_payload) → from PpaOutcomeTracker
    """

    def __init__(self) -> None:
        self._subs: dict[str, dict] = {}
        self._published: list[tuple[str, IncidentEvent | dict]] = []

    async def subscribe_raw(
        self, subject_pattern: str, *, handler, durable_name: str | None = None
    ) -> None:
        self._subs[subject_pattern] = {"handler": handler, "durable_name": durable_name}

    async def publish(
        self, arg1: IncidentEvent | str, arg2: dict | None = None
    ) -> None:
        if isinstance(arg1, IncidentEvent):
            subject = arg1.nats_subject()
            payload = arg1
        else:
            subject = arg1
            payload = arg2 if arg2 is not None else {}
        self._published.append((subject, payload))

    async def emit(self, subject: str, data: dict) -> None:
        """Simulate NATS delivering a message to all matching subscriptions."""
        for pattern, sub in self._subs.items():
            if self._subject_matches(pattern, subject):
                await sub["handler"](data, subject)

    @staticmethod
    def _subject_matches(pattern: str, subject: str) -> bool:
        if pattern.endswith(".>"):
            return subject.startswith(pattern[:-2])
        return subject == pattern

    @property
    def all_published_subjects(self) -> list[str]:
        return [s for s, _ in self._published]

    def subject_and_payload(self, index: int):
        return self._published[index]


# ── Shared bus wiring ──────────────────────────────────────────────────────────

class TestSharedNATSBus:
    """
    Integration point: all components share a single MockNATS instance.

    This verifies the wiring in a real system — Prescaler and PpaOutcomeTracker
    can coexist on the same bus. The unit tests cover component internals;
    these tests verify the integration surfaces.
    """

    @pytest.mark.asyncio
    async def test_prescaler_subscribes_with_correct_durable_name(self):
        """The NATS durable subscription name is a cross-component integration detail."""
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        # Exactly one subscription registered
        assert len(nats._subs) == 1
        pattern, sub = list(nats._subs.items())[0]
        assert pattern == "ppa.predictions.>"
        assert sub["durable_name"] == "prescaler-ppa-predictions"

    @pytest.mark.asyncio
    async def test_prediction_emits_to_correct_subject(self):
        """PPA prediction payload is delivered to all handlers matching the subject."""
        nats = MockNATS()
        events_received = []

        async def handler(data: dict, subject: str) -> None:
            events_received.append((data, subject))

        await nats.subscribe_raw("ppa.predictions.>", handler=handler)
        payload = make_ppa_payload(deployment="checkout-api")
        await nats.emit("ppa.predictions.checkout-api", payload)

        assert len(events_received) == 1
        data, subject = events_received[0]
        assert subject == "ppa.predictions.checkout-api"
        assert data["deployment"] == "checkout-api"
        assert data["predicted_rps"] == 450.0

    @pytest.mark.asyncio
    async def test_prescaler_and_tracker_coexist_on_same_bus(self, tmp_path):
        """
        Integration realism: Prescaler + PpaOutcomeTracker on shared MockNATS.

        The prescaler subscribes and makes shadow decisions. The tracker also
        lives on the same bus (in production it would receive ppa.predictions.*
        via its own subscription or callback). This test verifies they don't
        interfere.
        """
        nats = MockNATS()

        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        tracker = PpaOutcomeTracker(
            nats_client=nats,
            batch_output_path=tmp_path / "ppa_outcomes.jsonl",
        )

        payload = make_ppa_payload(
            deployment="checkout-api",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
        )

        # Emit prediction — prescaler receives via NATS subscription
        await nats.emit("ppa.predictions.checkout-api", payload)
        assert prescaler._decisions_made == 1, "Prescaler should record shadow decision"

        # Store in tracker (simulates external status-api passing event to tracker)
        await tracker.on_prediction(
            deployment="checkout-api",
            namespace="production",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
            horizon_minutes=5,
            model_version="v3.2.1",
            raw_features=payload["raw_features"],
            event_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        assert len(tracker._pending) == 1, "Tracker should store pending prediction"

    @pytest.mark.asyncio
    async def test_outcome_published_to_correct_subject(self, tmp_path):
        """OutcomeTracker publishes ppa.outcomes.{deployment} on verdict."""
        nats = MockNATS()
        tracker = PpaOutcomeTracker(nats_client=nats, batch_output_path=tmp_path / "out.jsonl")

        now = datetime.now(timezone.utc)
        tracker._pending["ID1"] = PendingPrediction(
            decision_id="ID1",
            deployment="api",
            namespace="default",
            predicted_rps=450.0,
            current_rps=120.0,
            confidence=0.85,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            expected_resolution_time=now - timedelta(seconds=1),
        )
        tracker._fetch_actual_rps = AsyncMock(return_value=480.0)
        await tracker._check_and_emit_outcome()

        outcome_subjects = [s for s, _ in nats._published if s.startswith("ppa.outcomes.")]
        assert len(outcome_subjects) == 1
        assert outcome_subjects[0] == "ppa.outcomes.api"


# ── FeedbackLoop wiring ────────────────────────────────────────────────────────

class TestFeedbackLoopBatchFlush:
    """
    FeedbackLoop must call ppa_outcome_tracker.run_batch_flush() on each cycle.

    This is the integration point between the FeedbackLoop and PpaOutcomeTracker.
    """

    @pytest.mark.asyncio
    async def test_flushed_called_each_cycle(self):
        flushed_signals: list = []

        class FakeTracker:
            async def run_batch_flush(self) -> None:
                flushed_signals.append("flushed")

        class FakeOutcomeStore:
            async def get_all_runbook_stats(self, days=30):
                return {}

            async def get_system_kpis(self, days=30):
                from nexus.learning.outcome_store import SystemKPIs
                return SystemKPIs(window_days=days)

            async def get_recent_outcomes(self, limit=50):
                return []

            async def get_targets_with_most_heals(self, days=7, limit=10):
                return []

        class FakeKnowledgeBase:
            async def bulk_update(self, stats):
                return {}

            async def get_all_adjustments(self):
                return {}

            async def record_pattern(self, **kwargs):
                pass

        class FakeScorer:
            def set_historical_boosts(self, boosts) -> None:
                pass

        loop = FeedbackLoop(
            outcome_store=FakeOutcomeStore(),
            knowledge_base=FakeKnowledgeBase(),
            confidence_scorer=FakeScorer(),
            ppa_outcome_tracker=FakeTracker(),
            interval_s=2.0,
            window_days=30,
        )

        await loop._update_cycle()
        assert flushed_signals == ["flushed"], "run_batch_flush should be called once"

        flushed_signals.clear()
        await loop._update_cycle()
        assert flushed_signals == ["flushed"], "run_batch_flush called every cycle"

    @pytest.mark.asyncio
    async def test_no_tracker_no_crash(self):
        """FeedbackLoop works when ppa_outcome_tracker is None (non-predictive deployments)."""

        class FakeOutcomeStore:
            async def get_all_runbook_stats(self, days=30):
                return {}

            async def get_system_kpis(self, days=30):
                from nexus.learning.outcome_store import SystemKPIs
                return SystemKPIs(window_days=days)

            async def get_recent_outcomes(self, limit=50):
                return []

            async def get_targets_with_most_heals(self, days=7, limit=10):
                return []

        class FakeKnowledgeBase:
            async def bulk_update(self, stats):
                return {}

            async def get_all_adjustments(self):
                return {}

            async def record_pattern(self, **kwargs):
                pass

        class FakeScorer:
            def set_historical_boosts(self, boosts) -> None:
                pass

        # ppa_outcome_tracker=None — simulate a non-predictive NEXUS install
        loop = FeedbackLoop(
            outcome_store=FakeOutcomeStore(),
            knowledge_base=FakeKnowledgeBase(),
            confidence_scorer=FakeScorer(),
            ppa_outcome_tracker=None,  # type: ignore[arg-type]
            interval_s=300.0,
            window_days=30,
        )

        # Must not raise
        await loop._update_cycle()
        assert loop.status["cycles_run"] == 1


# ── End-to-end sequence ───────────────────────────────────────────────────────

class TestPredictionOutcomeSequence:
    """
    Full PPA → NEXUS predictive plane sequence:
        PPA prediction event
      → Prescaler in shadow mode (decision logged)
      → PpaOutcomeTracker stores pending
      → Horizon expires → verdict published to ppa.outcomes.*
      → JSONL batch flush
    """

    @pytest.mark.asyncio
    async def test_prediction_to_outcome_to_jsonl(self, tmp_path):
        nats = MockNATS()

        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        tracker = PpaOutcomeTracker(
            nats_client=nats,
            batch_output_path=tmp_path / "ppa_outcomes.jsonl",
        )

        # 1. PPA operator publishes prediction (simulated — we emit directly)
        ppa_payload = make_ppa_payload(
            deployment="checkout-api",
            namespace="production",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
            horizon_minutes=5,
        )
        await nats.emit("ppa.predictions.checkout-api", ppa_payload)

        # 2. Prescaler records shadow decision
        assert prescaler._decisions_made == 1
        decision = prescaler._all_decisions[0]
        assert decision.deployment_name == "checkout-api"
        assert decision.predicted_rps == 900.0
        assert decision.executed is False  # shadow mode

        # 3. Store in PpaOutcomeTracker
        await tracker.on_prediction(
            deployment="checkout-api",
            namespace="production",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
            horizon_minutes=5,
            model_version="v3.2.1",
            raw_features=ppa_payload["raw_features"],
            event_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        assert len(tracker._pending) == 1

        # 4. Fast-forward: expire the prediction
        decision_id = list(tracker._pending.keys())[0]
        tracker._pending[decision_id] = PendingPrediction(
            decision_id=decision_id,
            deployment="checkout-api",
            namespace="production",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
            horizon_minutes=5,
            model_version="v3.2.1",
            raw_features={},
            expected_resolution_time=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        # 5. Outcome verdict published to ppa.outcomes.*
        tracker._fetch_actual_rps = AsyncMock(return_value=950.0)
        await tracker._check_and_emit_outcome()

        outcome_subjects = [s for s, _ in nats._published if s.startswith("ppa.outcomes.")]
        assert len(outcome_subjects) == 1
        subject, payload = nats._published[0]
        assert subject == "ppa.outcomes.checkout-api"
        assert payload["verdict"] == "spike_hit"
        assert payload["actual_rps"] == 950.0
        assert "ID1" not in tracker._pending  # cleared after emit

        # 6. Batch flush writes JSONL
        await tracker.run_batch_flush()

        output_file = tmp_path / "ppa_outcomes.jsonl"
        assert output_file.exists()
        lines = output_file.read_text().splitlines()
        assert len(lines) == 1
        record = _json.loads(lines[0])
        assert record["deployment"] == "checkout-api"
        assert record["verdict"] == "spike_hit"
        assert record["actual_rps"] == 950.0
        assert record["predicted_rps"] == 900.0
        assert len(tracker._recent_outcomes) == 0  # cleared after flush