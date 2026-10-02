"""
Prometheus Circuit Breaker & Feature Bounds
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

BOUNDS: dict[str, tuple[float, float]] = {
    "cpu_utilization_pct": (0.0, 200.0),
    "memory_utilization_pct": (0.0, 110.0),
    "error_rate": (0.0, 1.0),
    "rps": (0.0, 100_000.0),
    "latency_p95_ms": (0.0, 30_000.0),
}


class PrometheusCircuitBreaker:
    """
    Tri-state circuit breaker for Prometheus queries.

    CLOSED    -> normal, all queries flow through
    OPEN      -> too many failures, queries blocked for reset_timeout seconds
    HALF_OPEN -> trial query allowed after timeout; reverts to CLOSED on success
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(self, threshold: int = 5, reset_timeout: float = 60.0):
        self.threshold = threshold
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            if (
                self._opened_at
                and (time.monotonic() - self._opened_at) > self.reset_timeout
            ):
                self._state = self.HALF_OPEN
        return self._state

    def can_attempt(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self) -> None:
        if self._state == self.HALF_OPEN:
            logger.info("[MetricsAgent] Circuit breaker RESET -> CLOSED")
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            if self._state != self.OPEN:
                logger.warning(
                    f"[MetricsAgent] Circuit breaker TRIPPED -> OPEN "
                    f"({self._failures} consecutive failures)"
                )
            self._state = self.OPEN
            self._opened_at = time.monotonic()

    def __repr__(self) -> str:
        return f"CircuitBreaker(state={self.state}, failures={self._failures})"
