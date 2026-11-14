"""Governance Circuit Breaker.

Stops autonomous healing when consecutive post-check failures indicate
the healing system itself may be misconfigured or causing harm.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class GovernanceCircuitBreaker:
    """Tracks consecutive post-check failures across runbooks."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"

    def __init__(self, failure_threshold: int = 3, nats_client: Any = None):
        self._threshold = failure_threshold
        self._failures = 0
        self._state = self.CLOSED
        self._nats = nats_client
        self._tripped_at: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == self.OPEN

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def record_post_check_success(self) -> None:
        """Call after a runbook's post-checks pass."""
        prev = self._failures
        self._failures = 0
        self._state = self.CLOSED
        if prev > 0:
            logger.info(f"[GovernanceCB] RESET after {prev} consecutive failure(s)")

    def record_post_check_failure(self) -> None:
        """Call when a runbook's post-checks fail (healing didn't work)."""
        self._failures += 1
        if self._failures >= self._threshold:
            if self._state != self.OPEN:
                self._tripped_at = datetime.now(timezone.utc).isoformat()
                self._state = self.OPEN
                logger.critical(
                    f"[GovernanceCB] TRIPPED OPEN — {self._failures} consecutive "
                    f"post-check failures. Autonomous healing SUSPENDED."
                )

    def reset(self) -> None:
        """Manually reset the circuit breaker (human operator action)."""
        self._failures = 0
        self._state = self.CLOSED
        self._tripped_at = None
        logger.warning("[GovernanceCB] Manually RESET by operator")

    def status_dict(self) -> dict:
        return {
            "state": self._state,
            "consecutive_failures": self._failures,
            "threshold": self._threshold,
            "tripped_at": self._tripped_at,
        }
