"""
NEXUS Base Agent
================
Abstract foundation that every domain agent extends.

Every agent follows the same lifecycle:
    on_start()  →  run loop: [sense() → publish events]  →  on_stop()

The base class provides:
    • Periodic polling with configurable interval
    • Exponential backoff on consecutive sense() failures
    • Agent-level circuit breaker (emits CIRCUIT_BREAKER_TRIPPED after N failures)
    • Graceful shutdown via stop()
    • Uptime and event count tracking

Subclasses implement only sense() — returning a list of IncidentEvents.
They must NOT call nats.publish() directly from sense().
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import List, Optional

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Abstract base class for all NEXUS domain agents.

    Args:
        nats_client:            Connected NATSClient for event publishing.
        agent_type:             AgentType enum value identifying this agent.
        poll_interval_seconds:  How often sense() is called (default 30s).
        failure_threshold:      Consecutive sense() failures before circuit breaks (default 5).
        backoff_max_seconds:    Cap on exponential backoff sleep (default 300s).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        agent_type: AgentType,
        poll_interval_seconds: float = 30.0,
        failure_threshold: int = 5,
        backoff_max_seconds: float = 300.0,
    ):
        self.nats              = nats_client
        self.agent_type        = agent_type
        self.poll_interval     = poll_interval_seconds
        self.failure_threshold = failure_threshold
        self.backoff_max       = backoff_max_seconds

        self._running                = False
        self._consecutive_failures   = 0
        self._last_failure_time: Optional[float] = None
        self._total_events_published = 0
        self._start_time: Optional[float] = None

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    async def sense(self) -> List[IncidentEvent]:
        """
        Observe the agent's domain and return detected IncidentEvents.

        Contract:
            - Query data sources (Prometheus, K8s API, DB, NGINX logs, etc.)
            - Detect threshold breaches or anomalies
            - Return one IncidentEvent per detected condition
            - Return [] if everything is healthy or on transient errors
            - Do NOT call nats.publish() — the run loop does that
            - Do NOT raise exceptions for expected transient errors (timeouts, etc.)
              — catch them internally and return []
        """
        ...

    async def on_start(self) -> None:
        """Called once before the poll loop begins. Override for setup."""
        pass

    async def on_stop(self) -> None:
        """Called once after the poll loop ends. Override for cleanup."""
        pass

    # ── Run loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Start the poll loop. Runs until stop() is called or task is cancelled.

        Each cycle:
            1. Call sense() to observe the domain
            2. Publish each IncidentEvent to NATS
            3. Sleep for poll_interval (with exponential backoff on failures)
        """
        self._running    = True
        self._start_time = time.monotonic()
        name             = self._agent_name

        logger.info(f"[{name}] Starting (poll_interval={self.poll_interval}s)")
        await self.on_start()

        while self._running:
            cycle_start = time.monotonic()

            try:
                events = await self.sense()

                # Recovery — reset failure counter
                if self._consecutive_failures > 0:
                    logger.info(f"[{name}] Recovered after {self._consecutive_failures} failure(s)")
                    self._consecutive_failures = 0

                # Publish all events
                for event in events:
                    await self.nats.publish(event)
                    self._total_events_published += 1

                if events:
                    logger.info(f"[{name}] Published {len(events)} event(s)")

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self._consecutive_failures += 1
                self._last_failure_time = time.monotonic()
                logger.error(
                    f"[{name}] sense() error #{self._consecutive_failures}: {exc}",
                    exc_info=True,
                )
                # Emit circuit breaker event when threshold reached
                if self._consecutive_failures >= self.failure_threshold:
                    await self._emit_circuit_breaker(str(exc))

            # Sleep — respects exponential backoff and remaining cycle budget
            elapsed = time.monotonic() - cycle_start
            sleep   = max(0.0, self._compute_sleep() - elapsed)
            try:
                await asyncio.sleep(sleep)
            except asyncio.CancelledError:
                break

        logger.info(f"[{name}] Stopped. Total events published: {self._total_events_published}")
        await self.on_stop()

    def stop(self) -> None:
        """Signal the run loop to stop after the current cycle completes."""
        self._running = False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _compute_sleep(self) -> float:
        """Compute sleep with exponential backoff on consecutive failures."""
        if self._consecutive_failures == 0:
            return self.poll_interval
        # poll_interval * 2^(failures - 1), capped at backoff_max
        backoff = self.poll_interval * (2 ** (self._consecutive_failures - 1))
        return min(backoff, self.backoff_max)

    async def _emit_circuit_breaker(self, error: str) -> None:
        """Emit a CIRCUIT_BREAKER_TRIPPED event to notify the Orchestrator."""
        try:
            await self.nats.publish(IncidentEvent(
                agent=self.agent_type,
                signal_type=SignalType.CIRCUIT_BREAKER_TRIPPED,
                severity=Severity.CRITICAL,
                context={
                    "consecutive_failures": self._consecutive_failures,
                    "error_message":        error,
                    "backoff_seconds":      self._compute_sleep(),
                    "agent":                self._agent_name,
                },
            ))
        except Exception as exc:
            logger.error(f"[{self._agent_name}] Failed to emit circuit breaker event: {exc}")

    @property
    def _agent_name(self) -> str:
        v = self.agent_type
        return (v.value if hasattr(v, "value") else str(v)).capitalize() + "Agent"

    @property
    def uptime_seconds(self) -> float:
        return (time.monotonic() - self._start_time) if self._start_time else 0.0

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"poll={self.poll_interval}s, "
            f"running={self._running}, "
            f"events={self._total_events_published})"
        )
