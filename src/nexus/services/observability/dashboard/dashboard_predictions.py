"""
Prediction sentence builder and retrieval for the developer dashboard.
"""

from __future__ import annotations

from typing import Any


def make_prediction_sentence(d: Any) -> str:
    """Build a plain-English prediction sentence from a PrescaleDecision."""
    try:
        mult = d.predicted_rps / d.current_rps if d.current_rps > 0 else 1.0
        tables = getattr(d, "trigger_tables", []) or []
        table_clause = (
            f" — driven by reads to {', '.join(f'`{t}`' for t in tables[:3])}"
            if tables
            else ""
        )
        return (
            f"{d.deployment_name} is predicted to receive "
            f"{mult:.1f}x normal traffic "
            f"({d.current_rps:.0f} → {d.predicted_rps:.0f} RPS)"
            f"{table_clause}."
        )
    except Exception:
        return "Traffic spike predicted."


def get_predictions(prescaler: Any, app: str | None = None) -> list[dict[str, Any]]:
    """Retrieve and format the latest Prescale decisions."""
    if prescaler is None:
        return []

    predictions = []
    for d in reversed(prescaler._all_decisions[-20:]):
        if app and app.lower() not in (d.deployment_name or "").lower():
            continue

        multiplier = (d.predicted_rps / d.current_rps) if d.current_rps > 0 else 1.0

        predictions.append(
            {
                "deployment": d.deployment_name,
                "namespace": d.namespace,
                "current_rps": round(d.current_rps, 1),
                "predicted_rps": round(d.predicted_rps, 1),
                "multiplier": round(multiplier, 2),
                "confidence": round(d.confidence, 3),
                "mode": prescaler.stats.get("mode", "shadow"),
                "outcome": d.outcome or "pending",
                "decided_at": d.decided_at,
                "description": make_prediction_sentence(d),
            }
        )
    return predictions
