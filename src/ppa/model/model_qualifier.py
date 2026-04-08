"""Model qualification and promotion logic.

Handles decision-making for model promotion from experimental to production.
"""

import json
import os
from typing import Any, cast


def load_json(path: str) -> dict[str, object] | None:
    """Load JSON file, returning None if file doesn't exist."""
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return cast(dict[str, object], json.load(f))


def should_promote(
    champion_metrics: dict[str, Any] | None,
    challenger_metrics: dict[str, Any],
    metric: str = "smape",
    gate_threshold: float = 35.0,
    min_relative_improvement: float = 0.02,
    max_underprov_regression: float = 1.0,
) -> tuple[bool, str]:
    """Decide if challenger model should replace champion.

    Rules:
      1) challenger metric must pass gate_threshold
      2) if no champion exists -> promote (bootstrap)
      3) challenger must improve metric by min_relative_improvement
      4) challenger must not worsen ppa_under_prov_pct beyond max_underprov_regression

    Args:
        champion_metrics: Previous best model metrics (None if unseeded)
        challenger_metrics: New model metrics
        metric: Metric name to compare (default: "smape")
        gate_threshold: Maximum acceptable metric value (default: 35.0)
        min_relative_improvement: Minimum relative improvement to promote (default: 0.02)
        max_underprov_regression: Maximum acceptable under-provisioning regression (default: 1.0)

    Returns:
        Tuple of (should_promote: bool, reason: str)
    """
    challenger_metric = float(challenger_metrics.get(metric, float("inf")))
    if challenger_metric > gate_threshold:
        return (
            False,
            f"challenger failed gate: {metric}={challenger_metric:.2f} > {gate_threshold:.2f}",
        )

    if champion_metrics is None:
        return True, "no champion found (bootstrap promotion)"

    champion_metric = float(champion_metrics.get(metric, float("inf")))
    rel_improve = (champion_metric - challenger_metric) / max(abs(champion_metric), 1e-9)
    if rel_improve < min_relative_improvement:
        return False, (
            f"insufficient improvement: {metric} {champion_metric:.2f} -> {challenger_metric:.2f} "
            f"({rel_improve * 100:.2f}% < {min_relative_improvement * 100:.2f}%)"
        )

    champion_under = float(champion_metrics.get("ppa_under_prov_pct", 0.0))
    challenger_under = float(challenger_metrics.get("ppa_under_prov_pct", 0.0))
    if (challenger_under - champion_under) > max_underprov_regression:
        return False, (
            f"under-provisioning regression too high: "
            f"{champion_under:.2f}% -> {challenger_under:.2f}%"
        )

    return True, (
        f"better {metric}: {champion_metric:.2f} -> {challenger_metric:.2f} "
        f"and under-provisioning acceptable"
    )

__all__ = ["load_json", "should_promote"]
