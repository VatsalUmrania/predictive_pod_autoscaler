"""Precision and accuracy tracking for predictive decisions."""
from __future__ import annotations

from collections import deque

from nexus.predictive.models import PrecisionStats, PrescaleDecision


class PrecisionTracker:
    """Tracks shadow-mode prediction accuracy and SMAPE."""

    def __init__(self, window: int = 20, spike_threshold: float = 1.3):
        self._window = window
        self._threshold = spike_threshold
        self._decisions: dict[str, PrescaleDecision] = {}
        self._smape_deque: deque[float] = deque(maxlen=window)

    def record_decision(self, decision: PrescaleDecision) -> None:
        self._decisions[decision.decision_id] = decision

    def record_actual(self, decision_id: str, actual_rps: float) -> str | None:
        """Record the observed RPS after the horizon. Returns outcome."""
        decision = self._decisions.get(decision_id)
        if not decision:
            return None
        outcome = decision.mark_outcome(actual_rps, self._threshold)

        # SMAPE
        denom = abs(actual_rps) + abs(decision.predicted_rps)
        smape = 200.0 * abs(actual_rps - decision.predicted_rps) / max(denom, 1e-6)
        self._smape_deque.append(smape)

        return outcome

    def stats(self) -> PrecisionStats:
        recent = list(self._decisions.values())[-self._window :]
        tp = sum(1 for d in recent if d.outcome == "correct")
        fp = sum(1 for d in recent if d.outcome == "false_positive")
        pending = sum(1 for d in recent if d.outcome is None)
        return PrecisionStats(true_positives=tp, false_positives=fp, pending=pending)

    @property
    def rolling_smape(self) -> float | None:
        if not self._smape_deque:
            return None
        return sum(self._smape_deque) / len(self._smape_deque)

    def ready_for_advisory(
        self,
        min_samples: int = 20,
        min_precision: float = 0.70,
        max_smape: float = 25.0,
    ) -> bool:
        """Returns True if shadow-mode results justify promoting to ADVISORY."""
        st = self.stats()
        smape = self.rolling_smape
        return (
            st.sample_count >= min_samples
            and st.precision >= min_precision
            and (smape is None or smape <= max_smape)
        )
