"""
Integration tests for the full PPA → NEXUS prediction flow.

Covers the complete event journey:
    PPA Operator (_publish_prediction via PpaNATSClient)
    → NATS subject ppa.predictions.{deployment}
    → Prescaler (subscribe_to_ppa_predictions + handle_spike_prediction)
    → PpaOutcomeTracker (on_prediction + _check_and_emit_outcome + run_batch_flush)
    → NATS subject ppa.outcomes.{deployment}
    → FeedbackLoop (ppa_outcome_tracker wired, run_batch_flush called on cycle)

Uses in-process mock NATS. No real K8s or NATS server required.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from nexus.bus.incident_event import IncidentEvent
from nexus.learning.feedback_loop import FeedbackLoop

# Internal import
from nexus.learning.ppa_outcome_tracker import PendingPrediction, PpaOutcomeTracker
from nexus.predictive.prescaler import PrescaleMode, Prescaler

# ── Test fixtures ──────────────────────────────────────────────────────────────

def make_ppa_payload(
    deployment="payments-api",
    namespace="production",
    predicted_rps=450.0,
    current_rps=120.0,
    confidence=0.85,
    horizon_minutes=10,
    model_version="v3.2.1",
):
    return {
        "deployment": deployment,
        "namespace": namespace,
        "predicted_rps": predicted_rps,
        "current_rps": current_rps,
        "confidence": confidence,
        "horizon_minutes": horizon_minutes,
        "model_version": model_version,
        "raw_features": {"rps_per_replica": 40.0, "cpu_utilization_pct": 72.0},
    }


class MockNATS:
    """
    In-process mock NATS. Supports the NEXUSClient call surface used by tests:

    1. publish(IncidentEvent)                       → routed via nats_subject()
    2. publish(subject_str, dict_payload)          → raw subject path
    3. subscribe_raw(pattern, handler=...,           → captures subscriptions
                     durable_name=..., stream_name=...)
    4. _js  /  _nc aliases for self                 → JetStream and core NATS code
                                                       paths route back through
                                                       publish() so the same
                                                       assertions work
    """

    def __init__(self):
        self._subs: dict[str, dict] = {}
        self._published: list[tuple[str, IncidentEvent | dict]] = []
        # Alias `_js` and `_nc` to self so production code that prefers the
        # JetStream context (`self._nats._js.publish(...)`) or the raw NATS
        # core client (`self._nats._nc.publish(...)`) still routes through
        # MockNATS.publish instead of silently dropping the message.
        self._js = self
        self._nc = self

    async def subscribe_raw(
        self,
        subject_pattern: str,
        *,
        handler,
        durable_name: str | None = None,
        stream_name: str | None = None,
    ):
        self._subs[subject_pattern] = {
            "handler": handler,
            "durable_name": durable_name,
            "stream_name": stream_name,
        }

    async def publish(
        self,
        arg1: IncidentEvent | str,
        arg2: dict | bytes | None = None,
    ):
        """
        Handle both call signatures:
          - publish(event: IncidentEvent)          → Prescaler path (NATSClient interface)
          - publish(subject: str, payload: dict)   → PpaOutcomeTracker path
          - publish(subject: str, payload: bytes)  → RunbookExecutor / PpaOutcomeTracker
                                                     raw-publish path (json-encoded)

        Bytes payloads are JSON-decoded into dicts so list-style assertions can
        pattern-match fields (`payload["deployment"]`).
        """
        if isinstance(arg1, IncidentEvent):
            subject = arg1.nats_subject()
            payload = arg1
        else:
            subject = arg1
            if isinstance(arg2, bytes):
                try:
                    payload = json.loads(arg2.decode("utf-8"))
                except Exception:
                    payload = arg2
            else:
                payload = arg2 if arg2 is not None else {}
        self._published.append((subject, payload))

    async def emit(self, subject: str, data: dict):
        """Simulate a NATS message delivery to matched subscriptions."""
        for pattern, sub in self._subs.items():
            if self._subject_matches(pattern, subject):
                await sub["handler"](data, subject)

    @staticmethod
    def _subject_matches(pattern: str, subject: str) -> bool:
        """Simple wildcard matching: ppa.predictions.> matches ppa.predictions.api."""
        if pattern.endswith(".>"):
            prefix = pattern[:-2]
            return subject.startswith(prefix)
        return subject == pattern


# ── Prescaler + PPA prediction integration ─────────────────────────────────────

class TestPpaPredictionToPrescalerDecision:
    """Simulate the full flow: PPA publishes prediction → Prescaler handles it."""

    async def _make_prescaler(self) -> tuple[Prescaler, MockNATS]:
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()
        return prescaler, nats

    @pytest.mark.asyncio
    async def test_ppa_prediction_flows_through_prescaler_shadow_mode(self):
        """PPA publishes → NATS delivers → Prescaler records shadow decision."""
        prescaler, nats = await self._make_prescaler()

        payload = make_ppa_payload(
            deployment="payments-api",
            current_rps=120.0,
            predicted_rps=450.0,
            confidence=0.85,
            horizon_minutes=10,
        )

        await nats.emit("ppa.predictions.payments-api", payload)

        # Prescaler should have recorded a decision
        assert prescaler._decisions_made == 1
        decision = prescaler._all_decisions[0]
        assert decision.deployment_name == "payments-api"
        assert decision.current_rps == 120.0
        assert decision.predicted_rps == 450.0
        assert decision.confidence == 0.85
        assert decision.horizon_minutes == 10
        assert decision.mode == PrescaleMode.SHADOW
        assert decision.executed is False  # shadow mode — no execution

    @pytest.mark.asyncio
    async def test_prediction_below_confidence_threshold_is_skipped(self):
        """Low-confidence prediction should not produce a decision."""
        prescaler, nats = await self._make_prescaler()

        payload = make_ppa_payload(
            deployment="api",
            current_rps=100.0,
            predicted_rps=130.0,  # only 30% increase
            confidence=0.30,       # below 0.55 threshold
        )

        await nats.emit("ppa.predictions.api", payload)

        assert prescaler._decisions_made == 0
        assert prescaler._skipped_confidence == 1

    @pytest.mark.asyncio
    async def test_prediction_below_threshold_pct_is_skipped(self):
        """Prediction with < 30% increase is skipped."""
        prescaler, nats = await self._make_prescaler()

        payload = make_ppa_payload(
            deployment="api",
            current_rps=100.0,
            predicted_rps=125.0,  # only 25% increase
            confidence=0.80,
        )

        await nats.emit("ppa.predictions.api", payload)

        assert prescaler._decisions_made == 0
        assert prescaler._skipped_threshold == 1

    @pytest.mark.asyncio
    async def test_advisory_mode_publishes_nats_event(self):
        """ADVISORY mode publishes both:
        - the original pre_scale_advisory IncidentEvent on nexus.incidents.*
          (carries the human-action prompt "Run: nexus prescale approve <id>")
        - the Notifier-facing decision event on nexus.prescale.<deployment>.advisory
        """
        prescaler, nats = await self._make_prescaler()
        prescaler.promote(PrescaleMode.ADVISORY)

        payload = make_ppa_payload(deployment="checkout-api")
        await nats.emit("ppa.predictions.checkout-api", payload)

        assert prescaler._decisions_made == 1

        # Exactly two publishes: the advisory IncidentEvent + the prescale decision dict
        assert len(nats._published) == 2

        # Locate each by subject
        by_subject = {s: b for s, b in nats._published}
        advisory_body = by_subject["nexus.incidents.orchestrator.anomaly_predicted"]
        prescale_body = by_subject["nexus.prescale.checkout-api.advisory"]

        # Advisory IncidentEvent carries the human prompt
        if hasattr(advisory_body, "model_dump"):
            advisory_body = advisory_body.model_dump()
        assert advisory_body["context"]["type"] == "pre_scale_advisory"

        # nexus.prescale.* carries metadata for Notifier / dashboards
        assert prescale_body["deployment"] == "checkout-api"
        assert prescale_body["mode"] == "advisory"

    @pytest.mark.asyncio
    async def test_precision_tracker_records_decision(self):
        """Prescaler updates PrecisionTracker on every decision."""
        prescaler, nats = await self._make_prescaler()
        payload = make_ppa_payload(deployment="api")
        await nats.emit("ppa.predictions.api", payload)

        tracker = prescaler._tracker
        assert len(tracker._decisions) == 1
        decision_id = list(tracker._decisions.keys())[0]
        assert tracker._decisions[decision_id] is not None
        st = tracker.stats()
        # sample_count = TP + FP (outcomes resolved), pending counts unresolved
        assert st.sample_count == 0
        assert st.pending == 1  # outcome not yet recorded


# ── Prescaler → PpaOutcomeTracker integration ──────────────────────────────────

class TestPrescalerToPpaOutcomeTracker:
    """PpaOutcomeTracker receives predictions and emits outcomes after horizon."""

    @pytest.mark.asyncio
    async def test_on_prediction_stores_pending(self):
        """Calling on_prediction stores a PendingPrediction keyed by decision_id."""
        nats = MockNATS()
        tracker = PpaOutcomeTracker(nats_client=nats)

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
    async def test_outcome_emitted_after_horizon_expires(self):
        """
        After horizon_minutes elapse, _check_and_emit_outcome fetches actual RPS
        and publishes a verdict to ppa.outcomes.{deployment}.
        """
        nats = MockNATS()
        tracker = PpaOutcomeTracker(nats_client=nats)

        # Inject an already-expired pending prediction
        now = datetime.now(timezone.utc)
        tracker._pending["ID1"] = PendingPrediction(
            decision_id="ID1",
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

        # Mock Prometheus returning actual RPS that is a spike hit (>= 156)
        tracker._fetch_actual_rps = AsyncMock(return_value=480.0)
        await tracker._check_and_emit_outcome()

        # Should have published verdict
        assert len(nats._published) == 1
        subject, payload = nats._published[0]
        assert subject == "ppa.outcomes.api"
        assert payload["actual_rps"] == 480.0
        assert payload["predicted_rps"] == 450.0
        assert payload["verdict"] == "spike_hit"
        # Pending should be cleared
        assert "ID1" not in tracker._pending

    @pytest.mark.asyncio
    async def test_fresh_prediction_not_yet_resolved(self):
        """A prediction with future resolution time is not prematurely resolved."""
        nats = MockNATS()
        tracker = PpaOutcomeTracker(nats_client=nats)

        now = datetime.now(timezone.utc)
        tracker._pending["FRESH1"] = PendingPrediction(
            decision_id="FRESH1",
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

        # Nothing published, nothing fetched
        assert len(nats._published) == 0
        tracker._fetch_actual_rps.assert_not_called()
        assert "FRESH1" in tracker._pending

# ── Full end-to-end: PPA → NATS → Prescaler → OutcomeTracker ──────────────────

class TestFullPpaNexusPredictionFlow:
    """
    Simulate the complete cross-system event sequence:

    1. PPA Operator publishes ppa.predictions.checkout-api via PpaNATSClient
       (simulated — we emit directly on MockNATS)
    2. Prescaler subscribes, routes to handle_spike_prediction
    3. Prescaler shadow-mode logs the decision (no scale action)
    4. PpaOutcomeTracker stores the prediction
    5. Horizon expires → outcome published to ppa.outcomes.checkout-api
    6. FeedbackLoop cycle calls run_batch_flush → JSONL written
    """

    @pytest.mark.asyncio
    async def test_full_prediction_flow_shadow_mode(self, tmp_path):
        """
        End-to-end flow from prediction event to outcome record.
        Verifies all components interact correctly with the mock NATS bus.
        """
        # Setup shared mock NATS
        nats = MockNATS()

        # Components share the same NATS
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        outcome_tracker = PpaOutcomeTracker(
            nats_client=nats,
            batch_output_path=tmp_path / "ppa_outcomes.jsonl",
        )

        # Step 1: Emit PPA prediction via mock NATS (simulates PPA Operator)
        ppa_payload = make_ppa_payload(
            deployment="checkout-api",
            namespace="production",
            predicted_rps=900.0,
            current_rps=200.0,
            confidence=0.88,
            horizon_minutes=5,
        )

        await nats.emit("ppa.predictions.checkout-api", ppa_payload)

        # Step 2: Prescaler records shadow decision
        assert prescaler._decisions_made == 1
        decision = prescaler._all_decisions[0]
        assert decision.deployment_name == "checkout-api"
        assert decision.predicted_rps == 900.0
        assert decision.executed is False

        # Step 3: store in OutcomeTracker (simulates status_api callback)
        await outcome_tracker.on_prediction(
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
        assert len(outcome_tracker._pending) == 1

        # Step 4: Simulate horizon expiry (manually expire the prediction)
        decision_id = list(outcome_tracker._pending.keys())[0]
        outcome_tracker._pending[decision_id] = PendingPrediction(
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

        # Step 5: Outcome tracker emits verdict
        outcome_tracker._fetch_actual_rps = AsyncMock(return_value=950.0)
        await outcome_tracker._check_and_emit_outcome()

        # Verdict published to ppa.outcomes
        ppa_outcome_calls = [(s, p) for s, p in nats._published if s.startswith("ppa.outcomes.")]
        assert len(ppa_outcome_calls) >= 1
        subject, outcome_payload = ppa_outcome_calls[0]
        assert outcome_payload["verdict"] == "spike_hit"
        assert outcome_payload["actual_rps"] == 950.0

        # Step 6: batch flush writes JSONL
        await outcome_tracker.run_batch_flush()

        output_file = tmp_path / "ppa_outcomes.jsonl"
        assert output_file.exists()
        with open(output_file) as f:
            lines = f.readlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["deployment"] == "checkout-api"
        assert record["verdict"] == "spike_hit"
        # Buffer cleared after flush
        assert len(outcome_tracker._recent_outcomes) == 0

    @pytest.mark.asyncio
    async def test_precision_tracker_round_trip(self):
        """
        Verify PrecisionTracker accuracy after receiving outcome data
        from PpaOutcomeTracker (simulating the ppa.outcomes subscription).
        """
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        # Emit a spike prediction
        payload = make_ppa_payload(
            deployment="api",
            current_rps=100.0,
            predicted_rps=500.0,
            confidence=0.90,
        )
        await nats.emit("ppa.predictions.api", payload)

        decision_id = prescaler._all_decisions[0].decision_id

        # Simulate PpaOutcomeTracker verdict (spike hit)
        actual_rps = 520.0  # above 100 * 1.3 = 130 threshold
        outcome = prescaler._tracker.record_actual(decision_id, actual_rps)

        assert outcome == "correct"
        st = prescaler._tracker.stats()
        assert st.true_positives == 1
        assert st.false_positives == 0
        assert st.precision == 1.0

    @pytest.mark.asyncio
    async def test_precision_tracker_false_positive(self):
        """Verify false positive detection when spike did not materialise."""
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        payload = make_ppa_payload(
            deployment="api",
            current_rps=100.0,
            predicted_rps=500.0,
            confidence=0.90,
        )
        await nats.emit("ppa.predictions.api", payload)

        decision_id = prescaler._all_decisions[0].decision_id

        # Actual RPS is LOW — spike did not materialise
        outcome = prescaler._tracker.record_actual(decision_id, actual_rps=80.0)

        assert outcome == "false_positive"
        st = prescaler._tracker.stats()
        assert st.false_positives == 1
        assert st.true_positives == 0
        assert st.precision == 0.0

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_decisions(self):
        """Same deployment hit twice quickly → second decision is cooldown-blocked."""
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        payload = make_ppa_payload(deployment="api")

        await nats.emit("ppa.predictions.api", payload)
        assert prescaler._decisions_made == 1

        # Second prediction immediately after
        await nats.emit("ppa.predictions.api", payload)
        assert prescaler._decisions_made == 1  # blocked by cooldown
        assert prescaler._skipped_cooldown == 1

    @pytest.mark.asyncio
    async def test_multiple_deployments_tracked_independently(self):
        """Predictions for different deployments don't share cooldowns."""
        nats = MockNATS()
        prescaler = Prescaler(nats_client=nats, mode=PrescaleMode.SHADOW)
        await prescaler.subscribe_to_ppa_predictions()

        # Two different deployments
        p1 = make_ppa_payload(deployment="api-a")
        p2 = make_ppa_payload(deployment="api-b")

        await nats.emit("ppa.predictions.api-a", p1)
        await nats.emit("ppa.predictions.api-b", p2)

        assert prescaler._decisions_made == 2
        assert prescaler._all_decisions[0].deployment_name == "api-a"
        assert prescaler._all_decisions[1].deployment_name == "api-b"
        # No cooldowns triggered (different deployments)
        assert prescaler._skipped_cooldown == 0


# ── FeedbackLoop integration with PpaOutcomeTracker ─────────────────────────────

class TestFeedbackLoopWithPpaOutcomeTracker:
    """Verify FeedbackLoop calls run_batch_flush on each learning cycle."""

    @pytest.mark.asyncio
    async def test_feedback_loop_calls_ppa_tracker_batch_flush(self, tmp_path, monkeypatch):
        """On each _update_cycle, FeedbackLoop should call run_batch_flush."""
        # Capture whether run_batch_flush was called
        flushed = []

        class FakeOutcomeTracker:
            async def run_batch_flush(self):
                flushed.append(True)

        fake_tracker = FakeOutcomeTracker()

        # Patch OutcomeStore methods to avoid DB access
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
            def set_historical_boosts(self, boosts):
                pass

        loop = FeedbackLoop(
            outcome_store=FakeOutcomeStore(),
            knowledge_base=FakeKnowledgeBase(),
            confidence_scorer=FakeScorer(),
            ppa_outcome_tracker=fake_tracker,
            interval_s=2.0,
            window_days=30,
        )

        await loop._update_cycle()

        assert len(flushed) == 1, "run_batch_flush should be called once per cycle"

        flushed.clear()
        await loop._update_cycle()
        assert len(flushed) == 1, "run_batch_flush should be called each cycle"
