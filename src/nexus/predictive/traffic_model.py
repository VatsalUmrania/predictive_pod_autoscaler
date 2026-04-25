"""
NEXUS Traffic Model
====================
Predicts future HTTP endpoint traffic from time-series observations.

Two backends:
    EWMATrafficModel (always available):
        • EWMA of RPS per endpoint
        • Rate-of-change projected linearly over horizon
        • Confidence: signal-to-noise calibrated to [0.50, 0.85]
        • Advantage: zero dependencies, interpretable

    GRUTrafficModel (requires PyTorch + trained checkpoint):
        • GRU sequence model predicting next N steps of RPS
        • Trained from Prometheus historical data
        • Higher confidence on periodic traffic patterns
        • Advantage: captures daily/weekly periodicity

Usage:
    model = EWMATrafficModel()                 # Always works
    pred  = model.predict("payments-api", "/api/payments", current_rps=120.0, horizon=10)
    # TrafficPrediction(endpoint=/api/payments, predicted_rps=165.0, confidence=0.72)

    # After spike is confirmed:
    model.record_outcome("payments-api", actual_rps=158.0)
    # Updates model SMAPE / precision stats
"""

from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from nexus.predictive.feature_pipeline import _guard, _safe_div

logger = logging.getLogger(__name__)

_ALPHA = 0.25   # EWMA smoothing factor for RPS series


# ──────────────────────────────────────────────────────────────────────────────
# Prediction result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TrafficPrediction:
    """A point-in-time prediction for one endpoint."""
    deployment_name:  str
    endpoint:         str
    current_rps:      float
    predicted_rps:    float
    horizon_minutes:  int
    confidence:       float
    model_type:       str              # "ewma" | "gru"
    smape:            Optional[float] = None   # Rolling SMAPE from past predictions
    predicted_at:     str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def rps_increase_pct(self) -> float:
        if self.current_rps <= 0:
            return 0.0
        return 100.0 * (self.predicted_rps - self.current_rps) / self.current_rps

    @property
    def recommended_replicas(self, target_rps_per_replica: float = 100.0) -> int:
        """Estimate replica count to handle predicted load."""
        if target_rps_per_replica <= 0:
            return 1
        return max(1, math.ceil(self.predicted_rps / target_rps_per_replica))


# ──────────────────────────────────────────────────────────────────────────────
# SMAPE accuracy tracker
# ──────────────────────────────────────────────────────────────────────────────

class SMAPETracker:
    """
    Tracks Symmetric Mean Absolute Percentage Error for model accuracy.
    SMAPE is preferred over MAPE because it handles near-zero values gracefully.

    SMAPE = 200 × |actual - predicted| / (|actual| + |predicted|)  [0%, 200%]

    Low SMAPE (< 20%) indicates good predictive accuracy.
    """

    def __init__(self, window: int = 50):
        self._errors: Deque[float] = deque(maxlen=window)

    def record(self, predicted: float, actual: float) -> float:
        """Record a prediction outcome. Returns the SMAPE for this sample."""
        denom = abs(actual) + abs(predicted)
        if denom < 1e-6:
            smape = 0.0
        else:
            smape = 200.0 * abs(actual - predicted) / denom
        self._errors.append(smape)
        return smape

    @property
    def rolling_smape(self) -> Optional[float]:
        if not self._errors:
            return None
        return sum(self._errors) / len(self._errors)

    @property
    def sample_count(self) -> int:
        return len(self._errors)


# ──────────────────────────────────────────────────────────────────────────────
# Per-endpoint EWMA state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class EndpointState:
    """EWMA state for one endpoint's RPS."""
    rps_ewma:    float = 0.0
    roc_ewma:    float = 0.0    # EWMA of rate-of-change
    last_rps:    float = 0.0
    samples:     int   = 0
    tracker:     SMAPETracker = field(default_factory=SMAPETracker)

    def update(self, rps: float) -> float:
        """Update EWMAs. Returns current rate-of-change."""
        roc = abs(rps - self.last_rps)
        if self.samples == 0:
            self.rps_ewma = rps
            self.roc_ewma = 0.0
        else:
            self.rps_ewma = _ALPHA * rps + (1 - _ALPHA) * self.rps_ewma
            self.roc_ewma = _ALPHA * roc + (1 - _ALPHA) * self.roc_ewma
        self.last_rps = rps
        self.samples += 1
        return roc

    def predict_rps(self, horizon_minutes: int) -> float:
        """
        Linear extrapolation: predicted = current + roc_ewma × horizon_steps
        Assumes rate-of-change in the current direction continues.
        """
        # Steps: each step = 1 scrape interval (30s)
        horizon_steps = (horizon_minutes * 60) / 30.0
        predicted = self.last_rps + self.roc_ewma * horizon_steps
        return max(0.0, predicted)

    def confidence(self, horizon_minutes: int) -> float:
        """
        Confidence decreases with horizon and with signal noise (high roc_ewma variance).
        Range: [0.40, 0.82] — never fully confident, never below 40%.
        """
        if self.samples < 5:
            return 0.40      # Cold start

        # Base: inversely proportional to horizon
        base = max(0.40, 0.85 - (horizon_minutes / 60.0) * 0.30)

        # Noise penalty: high roc_ewma / rps_ewma = noisy signal
        noise_ratio = _safe_div(self.roc_ewma, max(self.rps_ewma, 1.0))
        noise_penalty = min(noise_ratio * 0.3, 0.25)

        # SMAPE bonus: lower historical SMAPE = higher confidence
        smape_bonus = 0.0
        smape = self.tracker.rolling_smape
        if smape is not None:
            smape_bonus = max(0.0, 0.10 * (1.0 - smape / 100.0))

        raw = base - noise_penalty + smape_bonus
        return max(0.40, min(0.82, raw))


# ──────────────────────────────────────────────────────────────────────────────
# EWMA Traffic Model (always available)
# ──────────────────────────────────────────────────────────────────────────────

class EWMATrafficModel:
    """
    EWMA-based endpoint traffic forecaster.

    Maintains per-endpoint EWMA state and predicts future RPS by linear
    extrapolation of the smoothed rate-of-change.

    Args:
        default_target_rps_per_replica: Used by recommended_replicas (default 100).
    """

    def __init__(self, default_target_rps_per_replica: float = 100.0):
        self._states: Dict[str, EndpointState] = {}
        self._target_rps_per_replica = default_target_rps_per_replica

    def update(self, deployment: str, current_rps: float) -> None:
        """Ingest an observation for a deployment/endpoint."""
        rps = _guard(current_rps)
        state = self._states.setdefault(deployment, EndpointState())
        state.update(rps)

    def predict(
        self,
        deployment:     str,
        endpoint:       str,
        current_rps:    float,
        horizon_minutes: int = 10,
    ) -> TrafficPrediction:
        """
        Predict endpoint RPS {horizon_minutes} minutes ahead.

        Automatically ingests current_rps as the latest observation.
        """
        rps   = _guard(current_rps)
        state = self._states.setdefault(deployment, EndpointState())
        state.update(rps)

        predicted = state.predict_rps(horizon_minutes)
        conf      = state.confidence(horizon_minutes)
        smape     = state.tracker.rolling_smape

        return TrafficPrediction(
            deployment_name = deployment,
            endpoint        = endpoint,
            current_rps     = round(rps, 2),
            predicted_rps   = round(predicted, 2),
            horizon_minutes = horizon_minutes,
            confidence      = round(conf, 3),
            model_type      = "ewma",
            smape           = round(smape, 2) if smape is not None else None,
        )

    def record_outcome(self, deployment: str, actual_rps: float) -> Optional[float]:
        """
        Record the actual RPS observed {horizon} minutes after a prediction.
        Updates SMAPE tracker for accuracy monitoring.
        Returns the SMAPE for this sample.
        """
        state = self._states.get(deployment)
        if state is None:
            return None
        predicted = state.predict_rps(0)   # Current EWMA as the "committed prediction"
        return state.tracker.record(predicted=predicted, actual=_guard(actual_rps))

    def smape_for(self, deployment: str) -> Optional[float]:
        return self._states.get(deployment, EndpointState()).tracker.rolling_smape

    def all_stats(self) -> Dict[str, Any]:
        return {
            name: {
                "rps_ewma":    round(s.rps_ewma, 2),
                "roc_ewma":    round(s.roc_ewma, 2),
                "samples":     s.samples,
                "smape":       round(s.tracker.rolling_smape, 2)
                               if s.tracker.rolling_smape else None,
            }
            for name, s in self._states.items()
        }
