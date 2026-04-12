"""State management for per-CR persistent data.

Moved from operator/main.py (Phase 2 refactoring).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ppa.operator.predictor import Predictor


@dataclass
class CRState:
    """Per-CR runtime state.

    Tracks the predictor, scaling decisions, and circuit breaker state
    for each PredictiveAutoscaler custom resource.

    Attributes:
        predictor: TFLite model + scaler + history for this CR
        observer_mode: Whether this CR is shadow-only and must not scale
        stable_count: Reconciliation cycles with stable replica count
        last_prediction: Last predicted load value
        last_desired: Last desired replica count (for stabilization)
        last_known_good_replicas: Last successful scaling decision
        last_known_good_prediction: Last successful prediction
        consecutive_failures: Consecutive reconciliation failures
        last_successful_cycle: Timestamp of last successful cycle
        prom_failures: Consecutive Prometheus failures (circuit breaker)
        prom_last_failure_time: Timestamp of last Prometheus failure
    """

    predictor: Predictor
    observer_mode: bool = False
    stable_count: int = 0
    last_prediction: float = 0.0
    last_desired: float = -1.0  # Replica target from previous cycle (stabilization anchor)
    # Graceful degradation tracking
    last_known_good_replicas: int = 0
    last_known_good_prediction: float = 0.0
    consecutive_failures: int = 0
    last_successful_cycle: float = 0.0
    # Per-CR circuit breaker state (PR#11)
    prom_failures: int = 0
    prom_last_failure_time: float = 0.0
    # Phase 3: Artifact loading and state observability
    artifact_load_failures: int = (
        0  # Consecutive artifact availability failures (resets on success)
    )
    using_legacy_artifacts: bool = (
        False  # Flag to track when legacy paths are in use (sticky on upgrade)
    )
    predictor_missing_logged: bool = (
        False  # Prevent log spam when predictor is None (transition-based)
    )
    deprecation_logged: bool = False  # Log deprecated CR fields once only
    target_scaler_missing_logged: bool = False  # Log missing target_scaler once only
