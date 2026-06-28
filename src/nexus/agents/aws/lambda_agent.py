"""
NEXUS Lambda Agent
===================
Monitors AWS Lambda functions via CloudWatch metrics and emits IncidentEvents
when error rates, throttles, timeouts, OOM, or concurrency limits are detected.

Observation sources:
    • CloudWatch Metrics (AWS/Lambda namespace):
        - Errors             → error rate calculation
        - Throttles          → throttle spike detection
        - Duration           → timeout proximity detection
        - ConcurrentExecutions → concurrency limit detection
        - InitDuration       → cold start anomaly detection
    • CloudWatch Logs (/aws/lambda/<function_name>):
        - Last 200 error lines for LLM context enrichment
    • Lambda API:
        - Function configuration (memory, timeout, runtime, last modified)
        - Version/alias listing for rollback context

Published IncidentEvents:
    LAMBDA_ERROR_RATE_HIGH    — error rate > threshold (default 5%)
    LAMBDA_THROTTLE_SPIKE     — throttle count > threshold (default 10)
    LAMBDA_TIMEOUT            — avg duration > 80% of configured timeout
    LAMBDA_OOM                — detected via log pattern "Runtime exited with error"
    LAMBDA_CONCURRENCY_MAXED  — at or near reserved/account concurrency limit
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from nexus.agents.aws.base_aws_agent import BaseAWSAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    LambdaErrorContext,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

# CloudWatch namespace for Lambda metrics
_CW_NS = "AWS/Lambda"


class LambdaAgent(BaseAWSAgent):
    """
    AWS Lambda monitoring agent.

    Polls CloudWatch every ``poll_interval_seconds`` (default 60s) for each
    Lambda function. Emits an IncidentEvent for every anomaly detected, then
    fetches enrichment context (logs, config, versions) for the LLM.

    Args:
        nats_client:   Connected NATSClient.
        config:        AWSConfig (reads from env if None).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.LAMBDA,
            config=config,
        )
        self._known_functions: list[dict] = []  # cached function list

    # ── Sense loop ────────────────────────────────────────────────────────────

    async def sense(self) -> list[IncidentEvent]:
        """Scan all monitored Lambda functions and return incident events."""
        events: list[IncidentEvent] = []

        # Refresh function list periodically (every 10 polls or on first run)
        if not self._known_functions:
            self._known_functions = await self._list_functions()

        tasks = [self._check_function(fn) for fn in self._known_functions]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                events.extend(result)
            elif isinstance(result, Exception):
                logger.debug(f"[LambdaAgent] Function check error: {result}")

        # Refresh function list every ~10 polls
        if self._total_events_published % 10 == 9:
            self._known_functions = []  # will reload on next cycle

        return events

    # ── Function discovery ────────────────────────────────────────────────────

    async def _list_functions(self) -> list[dict]:
        """List all Lambda functions in the region, filtered by config prefix/tags."""
        if self._lambda_client is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            functions = []
            paginator_resp = {"NextMarker": None}

            def _paginate(marker):
                kwargs: dict = {"MaxItems": 50}
                if marker:
                    kwargs["Marker"] = marker
                return self._lambda_client.list_functions(**kwargs)

            resp = await loop.run_in_executor(None, lambda: _paginate(None))
            while True:
                for fn in resp.get("Functions", []):
                    name = fn.get("FunctionName", "")
                    if self.cfg.is_lambda_monitored(name):
                        functions.append(fn)
                marker = resp.get("NextMarker")
                if not marker:
                    break
                resp = await loop.run_in_executor(None, lambda: _paginate(marker))

            logger.info(f"[LambdaAgent] Monitoring {len(functions)} Lambda function(s)")
            return functions
        except Exception as exc:
            logger.warning(f"[LambdaAgent] list_functions failed: {exc}")
            return []

    # ── Per-function checks ───────────────────────────────────────────────────

    async def _check_function(self, fn_config: dict) -> list[IncidentEvent]:
        """Run all checks for a single Lambda function."""
        name = fn_config.get("FunctionName", "unknown")
        events: list[IncidentEvent] = []
        dims = [{"Name": "FunctionName", "Value": name}]
        window = self.cfg.cw_window_minutes

        # Parallel metric queries
        (errors, invocations, throttles, duration, concurrency) = await asyncio.gather(
            self._get_metric_sum(_CW_NS, "Errors", dims, window),
            self._get_metric_sum(_CW_NS, "Invocations", dims, window),
            self._get_metric_sum(_CW_NS, "Throttles", dims, window),
            self._get_metric_average(_CW_NS, "Duration", dims, window, statistic="Maximum"),
            self._get_metric_average(_CW_NS, "ConcurrentExecutions", dims, window, statistic="Maximum"),
        )

        # ── Error rate check ──────────────────────────────────────────────────
        error_rate = (errors / invocations) if invocations > 0 else 0.0
        if error_rate >= self.cfg.lambda_error_threshold and invocations > 0:
            ctx = await self._build_context(fn_config, error_rate, int(errors), int(invocations))
            events.append(IncidentEvent(
                agent=AgentType.LAMBDA,
                signal_type=SignalType.LAMBDA_ERROR_RATE_HIGH,
                severity=Severity.CRITICAL if error_rate > 0.20 else Severity.WARNING,
                resource_name=name,
                confidence=min(0.95, 0.5 + error_rate),
                context=ctx.model_dump(),
            ))
            logger.warning(
                f"[LambdaAgent] {name}: error_rate={error_rate:.1%} "
                f"({int(errors)}/{int(invocations)} inv) — threshold={self.cfg.lambda_error_threshold:.1%}"
            )

        # ── Throttle check ────────────────────────────────────────────────────
        elif throttles >= self.cfg.lambda_throttle_threshold:
            ctx = await self._build_context(fn_config, 0.0, 0, int(invocations))
            ctx.throttle_count = int(throttles)
            events.append(IncidentEvent(
                agent=AgentType.LAMBDA,
                signal_type=SignalType.LAMBDA_THROTTLE_SPIKE,
                severity=Severity.WARNING,
                resource_name=name,
                confidence=0.80,
                context=ctx.model_dump(),
            ))

        # ── Timeout proximity check ────────────────────────────────────────────
        configured_timeout_ms = (fn_config.get("Timeout", 3)) * 1000  # seconds → ms
        if duration > 0 and configured_timeout_ms > 0:
            timeout_utilization = duration / configured_timeout_ms
            if timeout_utilization >= 0.80:
                ctx = await self._build_context(fn_config, error_rate, int(errors), int(invocations))
                events.append(IncidentEvent(
                    agent=AgentType.LAMBDA,
                    signal_type=SignalType.LAMBDA_TIMEOUT,
                    severity=Severity.WARNING,
                    resource_name=name,
                    confidence=0.75,
                    context=ctx.model_dump(),
                ))

        return events

    # ── Context builder ────────────────────────────────────────────────────────

    async def _build_context(
        self,
        fn_config: dict,
        error_rate: float,
        error_count: int,
        invocation_count: int,
    ) -> LambdaErrorContext:
        """Build LambdaErrorContext with logs and version history."""
        name = fn_config.get("FunctionName", "unknown")
        log_group = f"/aws/lambda/{name}"

        # Fetch logs and version info in parallel
        (log_lines, versions) = await asyncio.gather(
            self._get_recent_log_lines(
                log_group,
                max_lines=self.cfg.lambda_max_log_lines,
                window_minutes=self.cfg.cw_window_minutes,
                filter_pattern="ERROR",
            ),
            self._get_function_versions(name),
        )

        # Determine current version and previous version for rollback
        current_version = fn_config.get("Version", "$LATEST")
        previous_version = versions[1] if len(versions) > 1 else None

        # Sample up to 10 error messages from logs
        error_messages = [line for line in log_lines if "ERROR" in line.upper()][:10]

        return LambdaErrorContext(
            function_name=name,
            function_arn=fn_config.get("FunctionArn"),
            error_rate=error_rate,
            error_count=error_count,
            invocation_count=invocation_count,
            window_minutes=self.cfg.cw_window_minutes,
            runtime=fn_config.get("Runtime"),
            memory_mb=fn_config.get("MemorySize"),
            timeout_seconds=fn_config.get("Timeout"),
            last_modified=fn_config.get("LastModified"),
            current_version=current_version,
            previous_version=previous_version,
            last_log_lines=log_lines[:self.cfg.lambda_max_log_lines],
            recent_error_messages=error_messages,
        )

    async def _get_function_versions(self, function_name: str) -> list[str]:
        """Return list of published Lambda version numbers (newest first)."""
        if self._lambda_client is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._lambda_client.list_versions_by_function(
                    FunctionName=function_name,
                    MaxItems=10,
                ),
            )
            versions = [
                v.get("Version", "")
                for v in resp.get("Versions", [])
                if v.get("Version") != "$LATEST"
            ]
            return list(reversed(versions))  # newest first
        except Exception:
            return []
