"""Unit tests for ppa.bus.prediction_event.PpaPredictionEvent."""

from datetime import datetime, timezone

from ppa.bus.prediction_event import PpaPredictionEvent


class TestPpaPredictionEvent:
    def test_to_dict_all_fields_present(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        event = PpaPredictionEvent(
            deployment="payments-api",
            namespace="production",
            predicted_rps=450.0,
            current_rps=120.0,
            confidence=0.85,
            horizon_minutes=10,
            model_version="v3.2.1",
            raw_features={"rps_per_replica": 40.0, "cpu_utilization_pct": 72.0},
            timestamp=now.isoformat(),
        )
        d = event.to_dict()
        assert d["deployment"] == "payments-api"
        assert d["namespace"] == "production"
        assert d["predicted_rps"] == 450.0
        assert d["current_rps"] == 120.0
        assert d["confidence"] == 0.85
        assert d["horizon_minutes"] == 10
        assert d["model_version"] == "v3.2.1"
        assert d["raw_features"]["rps_per_replica"] == 40.0
        assert d["timestamp"] == now.isoformat()

    def test_to_nats_payload_is_bytes(self):
        event = PpaPredictionEvent(
            deployment="api",
            namespace="default",
            predicted_rps=50.0,
            current_rps=10.0,
            confidence=0.9,
            horizon_minutes=10,
            model_version="v1",
            raw_features={},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        payload = event.to_nats_payload()
        assert isinstance(payload, bytes)

    def test_to_nats_payload_roundtrips(self):
        import json

        event = PpaPredictionEvent(
            deployment="api",
            namespace="default",
            predicted_rps=50.0,
            current_rps=10.0,
            confidence=0.9,
            horizon_minutes=10,
            model_version="v1",
            raw_features={"cpu_utilization_pct": 50.0},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        parsed = json.loads(event.to_nats_payload())
        assert parsed["deployment"] == "api"
        assert parsed["predicted_rps"] == 50.0

    def test_raw_features_any_float_values(self):
        event = PpaPredictionEvent(
            deployment="svc",
            namespace="ns",
            predicted_rps=10.0,
            current_rps=5.0,
            confidence=0.75,
            horizon_minutes=10,
            model_version="v1",
            raw_features={
                "rps_per_replica": 10.5,
                "cpu_utilization_pct": 80.1,
                "latency_p95_ms": 150.0,
            },
            timestamp="2026-06-05T00:00:00+00:00",
        )
        d = event.to_dict()
        assert d["raw_features"]["rps_per_replica"] == 10.5
        assert len(d["raw_features"]) == 3
