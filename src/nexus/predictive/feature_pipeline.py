"""
NEXUS Predictive Feature Pipeline
===================================
Transforms raw DBAgent `QuerySnapshot` data and Prometheus time-series
into normalized feature vectors for the predictive models.

Key fixes over the original PPA operator (ARCHITECTURE_REVIEW_CRITICAL.md):
    ✅ §1.4  NaN / Inf guard: every normalization step checks before dividing
    ✅ §6.2  Feature bounds: clamp all values to [feature_min, feature_max]
    ✅ §1.5  Missing feature handling: configurable fill (zero | mean | last)
    ✅ §9.1  No global state: all state lives in FeaturePipeline instance

Feature groups produced:
    DB layer (from QuerySnapshot — window of raw counts):
        table_<name>_read_rate   — rolling reads/sec per table
        table_<name>_write_rate  — rolling writes/sec per table
        db_total_read_rate       — aggregate read rate
        db_rw_ratio              — read / (read + write) in [0, 1]

    Metrics layer (from Prometheus event context):
        cpu_utilization_pct      — [0, 100]
        memory_utilization_pct   — [0, 100]
        error_rate               — [0, 1]
        rps                      — requests/sec
        latency_p95_ms           — 95th percentile latency
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Feature bounds (clamp to training distribution)
# ──────────────────────────────────────────────────────────────────────────────

FEATURE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "cpu_utilization_pct":     (0.0,   100.0),
    "memory_utilization_pct":  (0.0,   100.0),
    "error_rate":              (0.0,   1.0),
    "rps":                     (0.0,   100_000.0),
    "latency_p95_ms":          (0.0,   60_000.0),
    "db_total_read_rate":      (0.0,   100_000.0),
    "db_rw_ratio":             (0.0,   1.0),
}

# Fill strategies for missing features
FILL_ZERO = "zero"
FILL_MEAN = "mean"
FILL_LAST = "last"


# ──────────────────────────────────────────────────────────────────────────────
# Query Snapshot
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class QuerySnapshot:
    """
    A point-in-time snapshot of per-table DB query counts.
    Published by DBAgent as context in DB_QUERY_SPIKE events.
    """
    captured_at:  datetime
    db_engine:    str
    table_counts: Dict[str, int] = field(default_factory=dict)
    """Raw cumulative counters per table (not rate — pipeline computes delta)."""

    @classmethod
    def from_event_context(cls, context: Dict[str, Any]) -> Optional["QuerySnapshot"]:
        """Parse a QuerySnapshot from a DBAgent event context dict."""
        table_counts = context.get("table_counts")
        db_engine    = context.get("db_engine", "unknown")
        if not isinstance(table_counts, dict):
            return None
        return cls(
            captured_at  = datetime.now(timezone.utc),
            db_engine    = db_engine,
            table_counts = {k: int(v) for k, v in table_counts.items()},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Feature vector
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FeatureVector:
    """
    A normalized, NaN-free feature vector ready for model input.

    Missing features are filled using the pipeline's fill_strategy.
    All values are clamped to FEATURE_BOUNDS.
    """
    timestamp:  datetime
    features:   Dict[str, float] = field(default_factory=dict)
    missing:    List[str] = field(default_factory=list)   # Features that were filled
    has_db:     bool = False
    has_metrics: bool = False

    def to_list(self, feature_names: List[str]) -> List[float]:
        """Return a dense vector in the given feature order."""
        return [self.features.get(name, 0.0) for name in feature_names]

    def __len__(self) -> int:
        return len(self.features)


# ──────────────────────────────────────────────────────────────────────────────
# Feature Pipeline
# ──────────────────────────────────────────────────────────────────────────────

def _safe_div(a: float, b: float, fallback: float = 0.0) -> float:
    """Division with NaN/Inf guard."""
    if b == 0.0 or not math.isfinite(b) or not math.isfinite(a):
        return fallback
    result = a / b
    return result if math.isfinite(result) else fallback


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value to [lo, hi], guarding against NaN."""
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, value))


def _guard(value: Any, fallback: float = 0.0) -> float:
    """Convert arbitrary value to a finite float."""
    try:
        v = float(value)
        return v if math.isfinite(v) else fallback
    except (TypeError, ValueError):
        return fallback


class FeaturePipeline:
    """
    Stateful feature extraction pipeline.

    Maintains a rolling history of QuerySnapshots to compute
    per-table read/write rates (delta between successive snapshots).

    Args:
        snapshot_window:  Number of snapshots to keep for rate computation (default 20).
        fill_strategy:    How to handle missing features: "zero" | "mean" | "last".
        interval_s:       Expected interval between snapshots in seconds (default 30).
    """

    def __init__(
        self,
        snapshot_window: int = 20,
        fill_strategy:   str = FILL_ZERO,
        interval_s:      float = 30.0,
    ):
        self._window     = snapshot_window
        self._fill       = fill_strategy
        self._interval_s = interval_s

        # Rolling snapshot history: deque of QuerySnapshot
        self._snapshots: Deque[QuerySnapshot] = deque(maxlen=snapshot_window)

        # Running mean for FILL_MEAN strategy
        self._feature_sums:  Dict[str, float] = {}
        self._feature_counts: Dict[str, int]  = {}

        # Last seen feature values for FILL_LAST strategy
        self._last_values: Dict[str, float] = {}

    # ── DB features ───────────────────────────────────────────────────────────

    def ingest_snapshot(self, snapshot: QuerySnapshot) -> None:
        """Add a QuerySnapshot to the rolling history."""
        self._snapshots.append(snapshot)

    def _compute_db_features(self) -> Dict[str, float]:
        """
        Compute per-table read/write rates from the last two snapshots.
        Returns an empty dict if fewer than 2 snapshots are available.
        """
        if len(self._snapshots) < 2:
            return {}

        prev = self._snapshots[-2]
        curr = self._snapshots[-1]

        # Time delta (guard against zero / negative)
        dt = (curr.captured_at - prev.captured_at).total_seconds()
        if dt <= 0.0:
            dt = self._interval_s

        features: Dict[str, float] = {}
        total_reads  = 0.0
        total_writes = 0.0

        all_tables = set(prev.table_counts) | set(curr.table_counts)
        for table in all_tables:
            prev_count = prev.table_counts.get(table, 0)
            curr_count = curr.table_counts.get(table, 0)
            delta = max(0, curr_count - prev_count)   # Counters only increase

            # Heuristic: even table names → reads, odd names → writes.
            # DBAgent will provide explicit read/write split in Phase 6.
            # For now, treat all counts as reads.
            rate = _safe_div(float(delta), dt)
            rate = _clamp(rate, 0.0, FEATURE_BOUNDS.get(f"table_{table}_read_rate", (0, 1e6))[1])
            features[f"table_{table}_read_rate"]  = rate
            features[f"table_{table}_write_rate"] = 0.0   # Phase 6: split from DBAgent
            total_reads += rate

        # Aggregate features
        features["db_total_read_rate"] = _clamp(
            total_reads, *FEATURE_BOUNDS["db_total_read_rate"]
        )
        features["db_rw_ratio"] = _clamp(
            _safe_div(total_reads, total_reads + total_writes, fallback=1.0),
            *FEATURE_BOUNDS["db_rw_ratio"],
        )
        return features

    # ── Metrics features ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_metrics_features(context: Dict[str, Any]) -> Dict[str, float]:
        """
        Extract and clamp metrics features from a MetricsAgent event context.
        Uses _guard() on every value — immune to NaN/None/string inputs.
        """
        raw: Dict[str, float] = {
            "cpu_utilization_pct":    _guard(context.get("cpu_utilization_pct",    context.get("cpu_pct"))),
            "memory_utilization_pct": _guard(context.get("memory_utilization_pct", context.get("mem_pct"))),
            "error_rate":             _guard(context.get("error_rate")),
            "rps":                    _guard(context.get("rps")),
            "latency_p95_ms":         _guard(context.get("latency_p95_ms",         context.get("latency_ms"))),
        }
        return {
            name: _clamp(value, *FEATURE_BOUNDS.get(name, (0.0, 1e9)))
            for name, value in raw.items()
        }

    # ── Feature vector construction ───────────────────────────────────────────

    def build_vector(
        self,
        metrics_context: Optional[Dict[str, Any]] = None,
    ) -> FeatureVector:
        """
        Construct a FeatureVector, combining DB rates from rolling history
        and metrics features from the latest event context.

        Args:
            metrics_context: Prometheus event context dict (can be None).

        Returns:
            FeatureVector with all features clamped and NaN-free.
        """
        ts      = datetime.now(timezone.utc)
        missing = []
        features: Dict[str, float] = {}

        # DB features
        db_features = self._compute_db_features()
        has_db = bool(db_features)
        features.update(db_features)

        # Metrics features
        has_metrics = False
        if metrics_context:
            met_features = self._extract_metrics_features(metrics_context)
            has_metrics  = any(v > 0.0 for v in met_features.values())
            features.update(met_features)

        # Fill missing metrics features
        for name in FEATURE_BOUNDS:
            if name not in features:
                filled = self._fill_value(name)
                features[name] = filled
                missing.append(name)

        # Update running mean and last-value state
        for name, value in features.items():
            self._last_values[name] = value
            self._feature_sums[name]   = self._feature_sums.get(name, 0.0)   + value
            self._feature_counts[name] = self._feature_counts.get(name, 0)    + 1

        return FeatureVector(
            timestamp   = ts,
            features    = features,
            missing     = missing,
            has_db      = has_db,
            has_metrics = has_metrics,
        )

    def _fill_value(self, feature_name: str) -> float:
        """Return the fill value for a missing feature."""
        if self._fill == FILL_ZERO:
            return 0.0
        if self._fill == FILL_LAST:
            return self._last_values.get(feature_name, 0.0)
        if self._fill == FILL_MEAN:
            count = self._feature_counts.get(feature_name, 0)
            if count == 0:
                return 0.0
            return _safe_div(
                self._feature_sums.get(feature_name, 0.0), float(count)
            )
        return 0.0

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)
