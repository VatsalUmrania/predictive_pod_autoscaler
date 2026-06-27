"""PpaPredictionEvent — structured prediction payload for PPA → NEXUS NATS bus."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class PpaPredictionEvent:
    """
    Emitted by PPA's observer-mode ScalerStateMachine to NATS subject
    ``ppa.predictions.{deployment}`` on every 30-second reconciliation cycle.

    Fields:
        deployment:       Target K8s Deployment name.
        namespace:        Target namespace.
        predicted_rps:    Raw LSTM output — requests/sec predicted for the horizon.
        current_rps:      Observed requests/sec at prediction time.
        confidence:       [0, 1] from predictor metadata / concept-drift score.
        horizon_minutes:  Prediction lookback horizon (usually 10 minutes).
        model_version:    Identifying tag for the active LSTM model bundle.
        raw_features:     All 14 Prometheus feature values for NEXUS RCA enrichment.
        timestamp:        ISO-8601 UTC timestamp of emission.
    """

    deployment: str
    namespace: str
    predicted_rps: float
    current_rps: float
    confidence: float
    horizon_minutes: int
    model_version: str
    raw_features: dict[str, float]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment": self.deployment,
            "namespace": self.namespace,
            "predicted_rps": self.predicted_rps,
            "current_rps": self.current_rps,
            "confidence": self.confidence,
            "horizon_minutes": self.horizon_minutes,
            "model_version": self.model_version,
            "raw_features": self.raw_features,
            "timestamp": self.timestamp,
        }

    def to_nats_payload(self) -> bytes:
        return json.dumps(self.to_dict()).encode("utf-8")
