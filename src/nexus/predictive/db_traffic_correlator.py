"""
NEXUS DB Traffic Correlator
=============================
Uses DB query patterns from DBAgent to predict HTTP endpoint traffic spikes
5–15 minutes before they manifest in Prometheus metrics.

Core insight:
    DB reads are a leading indicator of HTTP load. When a user requests
    /api/orders, the orders and products tables are read. By watching
    the rate-of-change of per-table query counts, we can detect surges
    before they saturate CPU/memory.

Pipeline:
    DBAgent → DB_QUERY_SPIKE events (QuerySnapshot in context)
        ↓
    DBTrafficCorrelator.ingest(event)
        ↓
    FeaturePipeline → per-table read rates (EWMA-smoothed)
        ↓
    SpikeDetector → rate-of-change > spike_multiplier × EWMA baseline
        ↓
    TableEndpointMapper → {"orders": "/api/orders"} (configurable)
        ↓
    TRAFFIC_SPIKE_PREDICTED event → NATS

Table-to-endpoint mapping:
    The mapping file (nexus_traffic_map.yaml) is hot-reloadable.
    Default fallback: /api/<table_name>

Configuration (environment variables):
    NEXUS_SPIKE_MULTIPLIER          Rate-of-change threshold (default 2.5×)
    NEXUS_SPIKE_PREDICTION_HORIZON  Minutes ahead to predict (default 10)
    NEXUS_TRAFFIC_MAP_PATH          Path to YAML mapping file
    NEXUS_CORRELATOR_WINDOW         Number of snapshots in EWMA window (default 20)
"""

from __future__ import annotations

import logging
import math
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import yaml

from nexus.bus.incident_event import (
    AgentType, IncidentEvent, Severity, SignalType,
    TrafficSpikePredictionContext,
)
from nexus.bus.nats_client import NATSClient
from nexus.predictive.feature_pipeline import FeaturePipeline, QuerySnapshot

logger = logging.getLogger(__name__)

# EWMA smoothing factor α ∈ (0, 1] — higher = more reactive
_ALPHA_EWMA = 0.3


# ──────────────────────────────────────────────────────────────────────────────
# Table → Endpoint Mapper
# ──────────────────────────────────────────────────────────────────────────────

class TableEndpointMapper:
    """
    Maps DB table names to HTTP endpoints using a YAML config file.

    YAML format (nexus_traffic_map.yaml):
        table_endpoint_map:
          users:    /api/users
          orders:   /api/orders
          products: /api/products

    Hot-reloadable: call reload() after the file changes.
    Falls back to /api/<table_name> if no mapping found.
    """

    def __init__(self, map_path: Optional[Path] = None):
        self._path       = map_path
        self._mapping:   Dict[str, str] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self._mapping = raw.get("table_endpoint_map", {})
            logger.info(
                f"[TableEndpointMapper] Loaded {len(self._mapping)} table→endpoint mappings"
            )
        except Exception as exc:
            logger.warning(f"[TableEndpointMapper] Failed to load {self._path}: {exc}")

    def reload(self) -> None:
        self._mapping.clear()
        if self._path and self._path.exists():
            self._load()

    def get_endpoint(self, table_name: str) -> str:
        """Return mapped endpoint or fall back to /api/<table_name>."""
        return self._mapping.get(table_name, f"/api/{table_name}")

    def add(self, table: str, endpoint: str) -> None:
        """Runtime override — useful for tests."""
        self._mapping[table] = endpoint


# ──────────────────────────────────────────────────────────────────────────────
# Per-table rate tracker (EWMA-based spike detection)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TableRateState:
    """EWMA state for one DB table's query rate."""
    table:          str
    rate_ewma:      float = 0.0   # EWMA of query rate
    roc_ewma:       float = 0.0   # EWMA of rate-of-change
    last_rate:      float = 0.0
    samples:        int   = 0
    last_spiked_at: Optional[datetime] = None

    def update(self, new_rate: float) -> float:
        """
        Update EWMA state with new rate.
        Returns the rate-of-change (delta from last sample).
        """
        roc = abs(new_rate - self.last_rate)

        if self.samples == 0:
            self.rate_ewma = new_rate
            self.roc_ewma  = 0.0
        else:
            self.rate_ewma = _ALPHA_EWMA * new_rate + (1 - _ALPHA_EWMA) * self.rate_ewma
            self.roc_ewma  = _ALPHA_EWMA * roc      + (1 - _ALPHA_EWMA) * self.roc_ewma

        self.last_rate = new_rate
        self.samples  += 1
        return roc

    def is_spiking(
        self,
        current_roc:       float,
        spike_multiplier:  float,
        cooldown_seconds:  float = 300.0,
    ) -> bool:
        """
        Returns True if the current rate-of-change exceeds the EWMA baseline
        by spike_multiplier, and the last spike was more than cooldown_seconds ago.
        """
        # Need baseline to avoid false positives on cold start
        if self.samples < 5:
            return False

        baseline = self.roc_ewma
        if baseline < 1.0:
            return False    # Absolute minimum activity threshold

        if current_roc < spike_multiplier * baseline:
            return False

        # Cooldown
        if self.last_spiked_at:
            elapsed = (datetime.now(timezone.utc) - self.last_spiked_at).total_seconds()
            if elapsed < cooldown_seconds:
                return False

        return True

    def confidence(self, spike_ratio: float) -> float:
        """
        Confidence score 0.0–1.0 proportional to how much the spike exceeds baseline.
        Capped at 0.90 (reserve > 0.90 for validated multi-signal spikes).
        """
        if spike_ratio <= 1.0 or not math.isfinite(spike_ratio):
            return 0.0
        # sigmoid-like mapping: 2x baseline → 0.50, 5x → 0.85, 10x → 0.90
        raw = 1.0 - (1.0 / (1.0 + (spike_ratio - 1.0) * 0.25))
        return min(raw * 0.95, 0.90)


# ──────────────────────────────────────────────────────────────────────────────
# Spike prediction
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SpikePrediction:
    """Result of spike detection for one table/endpoint."""
    table:              str
    endpoint:           str
    namespace:          str
    current_rate:       float
    spike_ratio:        float          # current_roc / baseline_roc
    confidence:         float
    horizon_minutes:    int
    predicted_rps:      float          # Extrapolated endpoint RPS


# ──────────────────────────────────────────────────────────────────────────────
# DB Traffic Correlator
# ──────────────────────────────────────────────────────────────────────────────

class DBTrafficCorrelator:
    """
    Correlates DB query rate spikes to HTTP endpoint traffic predictions.

    Args:
        nats_client:       Connected NATSClient to publish predictions.
        namespace:         K8s namespace this correlator watches.
        spike_multiplier:  Rate-of-change threshold as multiple of EWMA baseline (default 2.5).
        horizon_minutes:   Minutes ahead to project spike (default 10).
        cooldown_seconds:  Minimum seconds between predictions for the same table (default 300).
        map_path:          Path to nexus_traffic_map.yaml (optional).
    """

    def __init__(
        self,
        nats_client:      NATSClient,
        namespace:        str = "default",
        spike_multiplier: float = 2.5,
        horizon_minutes:  int = 10,
        cooldown_seconds: float = 300.0,
        map_path:         Optional[Path] = None,
    ):
        self._nats            = nats_client
        self._namespace       = namespace
        self._spike_mult      = float(os.getenv("NEXUS_SPIKE_MULTIPLIER", str(spike_multiplier)))
        self._horizon         = int(os.getenv("NEXUS_SPIKE_PREDICTION_HORIZON", str(horizon_minutes)))
        self._cooldown        = cooldown_seconds
        self._mapper          = TableEndpointMapper(map_path)
        self._pipeline        = FeaturePipeline(
            snapshot_window=int(os.getenv("NEXUS_CORRELATOR_WINDOW", "20"))
        )

        # Per-table EWMA state
        self._table_state: Dict[str, TableRateState] = {}

        # Stats
        self._snapshots_ingested = 0
        self._spikes_predicted   = 0

    # ── Ingestion ─────────────────────────────────────────────────────────────

    async def ingest_db_event(self, event: IncidentEvent) -> None:
        """
        Process a DB_QUERY_SPIKE event from DBAgent.
        If a spike is detected, publish TRAFFIC_SPIKE_PREDICTED.
        """
        if event.signal_type != SignalType.DB_QUERY_SPIKE:
            return

        snapshot = QuerySnapshot.from_event_context(event.context)
        if snapshot is None:
            return

        self._pipeline.ingest_snapshot(snapshot)
        self._snapshots_ingested += 1

        # Build feature vector (DB-only at this point)
        fv = self._pipeline.build_vector()
        if not fv.has_db:
            return

        # Evaluate each table
        predictions: List[SpikePrediction] = []
        for table, rate in {
            k[len("table_"):][: -len("_read_rate")]: v
            for k, v in fv.features.items()
            if k.startswith("table_") and k.endswith("_read_rate")
        }.items():
            state = self._table_state.setdefault(table, TableRateState(table=table))
            roc   = state.update(rate)

            if state.is_spiking(roc, self._spike_mult, self._cooldown):
                spike_ratio = roc / max(state.roc_ewma, 1.0)
                confidence  = state.confidence(spike_ratio)
                endpoint    = self._mapper.get_endpoint(table)

                # Extrapolate RPS: linear projection from current rate
                # (current_rate × spike_ratio projected over horizon)
                predicted_rps = rate * max(spike_ratio, 2.0) * (self._horizon / 10.0)

                predictions.append(SpikePrediction(
                    table           = table,
                    endpoint        = endpoint,
                    namespace       = self._namespace,
                    current_rate    = rate,
                    spike_ratio     = spike_ratio,
                    confidence      = confidence,
                    horizon_minutes = self._horizon,
                    predicted_rps   = predicted_rps,
                ))
                state.last_spiked_at = datetime.now(timezone.utc)

        # Publish one prediction event per detected spike
        for pred in predictions:
            await self._publish_prediction(pred, event)
            self._spikes_predicted += 1

    # ── Publishing ────────────────────────────────────────────────────────────

    async def _publish_prediction(
        self, pred: SpikePrediction, source_event: IncidentEvent
    ) -> None:
        ctx = TrafficSpikePredictionContext(
            endpoint=pred.endpoint,
            predicted_rps=round(pred.predicted_rps, 2),
            current_rps=round(pred.current_rate, 2),
            prediction_horizon_minutes=pred.horizon_minutes,
            db_table_trigger=pred.table,
            confidence=round(pred.confidence, 3),
        )

        evt = IncidentEvent(
            agent         = AgentType.ORCHESTRATOR,   # Predictive plane sub-system
            signal_type   = SignalType.TRAFFIC_SPIKE_PREDICTED,
            severity      = Severity.WARNING,
            namespace     = pred.namespace,
            resource_name = pred.endpoint,
            correlation_id = source_event.correlation_id or source_event.event_id,
            context       = ctx.model_dump(),
            confidence    = pred.confidence,
        )

        await self._nats.publish(evt)
        logger.info(
            f"[DBTrafficCorrelator] ↑ TRAFFIC_SPIKE_PREDICTED "
            f"table={pred.table} endpoint={pred.endpoint} "
            f"ratio={pred.spike_ratio:.1f}× "
            f"conf={pred.confidence:.2f} "
            f"horizon={pred.horizon_minutes}min"
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "snapshots_ingested": self._snapshots_ingested,
            "spikes_predicted":   self._spikes_predicted,
            "tables_tracked":     len(self._table_state),
            "pipeline_snapshots": self._pipeline.snapshot_count,
            "spike_multiplier":   self._spike_mult,
            "horizon_minutes":    self._horizon,
        }
