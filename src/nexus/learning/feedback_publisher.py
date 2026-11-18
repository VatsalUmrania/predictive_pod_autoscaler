"""
NATS event publication for NEXUS Feedback Loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from nexus.bus.events import AgentType, IncidentEvent, Severity, SignalType
from nexus.learning.outcome_models import SystemKPIs

logger = logging.getLogger(__name__)


async def publish_learning_cycle_summary(
    nats_client: Any,
    cycles_run: int,
    kpis: SystemKPIs,
    recs: list[Any],
    adjustments: dict[str, float],
    cycle_ms: int,
) -> None:
    """Publish a LEARNING_CYCLE_COMPLETE event to NATS."""
    if not nats_client:
        return
    try:
        evt = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=SignalType.ANOMALY_DETECTED,
            severity=Severity.INFO,
            context={
                "type": "learning_cycle_complete",
                "cycle_number": cycles_run,
                "cycle_ms": cycle_ms,
                "system_kpis": kpis.to_dict(),
                "adjustments": {k: round(v, 4) for k, v in adjustments.items()},
                "recommendations": [r.to_dict() for r in recs[:10]],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        await nats_client.publish(evt)
    except Exception as exc:
        logger.debug(f"[FeedbackLoop] NATS publish failed: {exc}")
