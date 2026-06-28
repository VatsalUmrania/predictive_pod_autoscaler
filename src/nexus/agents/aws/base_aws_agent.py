"""
NEXUS Base AWS Agent
=====================
Abstract extension of BaseAgent that adds boto3 client management
and shared CloudWatch/Logs/X-Ray helpers used by all AWS domain agents.

Design:
    • All AWS clients are created lazily (first sense() call) to avoid
      slow imports at module load time.
    • Credentials come from boto3's standard chain: env vars → ~/.aws/credentials
      → IAM role attached to the EC2/ECS/Lambda execution environment.
    • Each subclass calls ``self._cw`` / ``self._logs`` / ``self._xray`` /
      ``self._lambda_client`` / ``self._sqs`` / ``self._dynamodb`` to get
      the relevant client, creating it on demand.

All helpers return empty results on any error — they never raise.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus.agents.base_agent import BaseAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import AgentType
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

# Try importing boto3; graceful failure if not installed
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception    # type: ignore[assignment,misc]
    _BOTO3_AVAILABLE = False


class BaseAWSAgent(BaseAgent, ABC):
    """
    Abstract base for all AWS domain agents.

    Extends BaseAgent with:
      • AWSConfig integration (region, thresholds, filters)
      • Lazy boto3 client creation with thread-safe caching
      • Shared CloudWatch metric query helpers
      • Shared CloudWatch Logs helpers
      • Graceful handling when boto3 is not installed

    Args:
        nats_client:          Connected NATSClient.
        agent_type:           AgentType enum value.
        config:               AWSConfig (reads from env if None).
        poll_interval_seconds: How often sense() is called (default from config).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        agent_type: AgentType,
        config: AWSConfig | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        cfg = config or AWSConfig.from_env()
        super().__init__(
            nats_client=nats_client,
            agent_type=agent_type,
            poll_interval_seconds=poll_interval_seconds or cfg.poll_interval_seconds,
        )
        self.cfg = cfg
        self._region = cfg.region

        # Lazy boto3 clients — None until first access
        self.__cw: Any = None
        self.__logs: Any = None
        self.__xray: Any = None
        self.__lambda: Any = None
        self.__sqs: Any = None
        self.__dynamodb: Any = None
        self.__apigw: Any = None

    # ── Boto3 client properties (lazy) ────────────────────────────────────────

    @property
    def _cw(self):
        """CloudWatch metrics client."""
        if self.__cw is None:
            self.__cw = self._make_client("cloudwatch")
        return self.__cw

    @property
    def _logs(self):
        """CloudWatch Logs client."""
        if self.__logs is None:
            self.__logs = self._make_client("logs")
        return self.__logs

    @property
    def _xray(self):
        """AWS X-Ray client."""
        if self.__xray is None:
            self.__xray = self._make_client("xray")
        return self.__xray

    @property
    def _lambda_client(self):
        """AWS Lambda client."""
        if self.__lambda is None:
            self.__lambda = self._make_client("lambda")
        return self.__lambda

    @property
    def _sqs(self):
        """AWS SQS client."""
        if self.__sqs is None:
            self.__sqs = self._make_client("sqs")
        return self.__sqs

    @property
    def _dynamodb(self):
        """AWS DynamoDB client."""
        if self.__dynamodb is None:
            self.__dynamodb = self._make_client("dynamodb")
        return self.__dynamodb

    @property
    def _apigw(self):
        """AWS API Gateway client."""
        if self.__apigw is None:
            self.__apigw = self._make_client("apigateway")
        return self.__apigw

    def _make_client(self, service: str):
        """Create a boto3 client. Returns None if boto3 is unavailable."""
        if not _BOTO3_AVAILABLE or boto3 is None:
            logger.warning(f"[{self._agent_name}] boto3 not available — install ppa[aws]")
            return None
        try:
            return boto3.client(service, region_name=self._region)
        except Exception as exc:
            logger.error(f"[{self._agent_name}] Failed to create boto3 client for {service}: {exc}")
            return None

    # ── CloudWatch Metric Helpers ──────────────────────────────────────────────

    async def _get_metric_sum(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict],
        window_minutes: int | None = None,
        period_seconds: int = 60,
    ) -> float:
        """
        Get the Sum statistic for a CloudWatch metric over the past window_minutes.
        Returns 0.0 on any error.
        """
        if self._cw is None:
            return 0.0
        window = window_minutes or self.cfg.cw_window_minutes
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=window)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._cw.get_metric_statistics(
                    Namespace=namespace,
                    MetricName=metric_name,
                    Dimensions=dimensions,
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=period_seconds * window,
                    Statistics=["Sum"],
                ),
            )
            datapoints = resp.get("Datapoints", [])
            return sum(dp.get("Sum", 0.0) for dp in datapoints)
        except (BotoCoreError, ClientError, Exception) as exc:
            logger.debug(f"[{self._agent_name}] _get_metric_sum({metric_name}): {exc}")
            return 0.0

    async def _get_metric_average(
        self,
        namespace: str,
        metric_name: str,
        dimensions: list[dict],
        window_minutes: int | None = None,
        period_seconds: int = 60,
        statistic: str = "Average",
    ) -> float:
        """
        Get the Average (or other statistic) for a CloudWatch metric.
        Returns 0.0 on any error.
        """
        if self._cw is None:
            return 0.0
        window = window_minutes or self.cfg.cw_window_minutes
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=window)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._cw.get_metric_statistics(
                    Namespace=namespace,
                    MetricName=metric_name,
                    Dimensions=dimensions,
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=period_seconds * window,
                    Statistics=[statistic],
                ),
            )
            datapoints = resp.get("Datapoints", [])
            if not datapoints:
                return 0.0
            return sum(dp.get(statistic, 0.0) for dp in datapoints) / len(datapoints)
        except (BotoCoreError, ClientError, Exception) as exc:
            logger.debug(f"[{self._agent_name}] _get_metric_average({metric_name}): {exc}")
            return 0.0

    # ── CloudWatch Logs Helpers ────────────────────────────────────────────────

    async def _get_recent_log_lines(
        self,
        log_group: str,
        max_lines: int = 200,
        window_minutes: int = 5,
        filter_pattern: str = "ERROR",
    ) -> list[str]:
        """
        Fetch recent log lines from a CloudWatch Logs group.
        Returns [] on any error or if the log group doesn't exist.
        """
        if self._logs is None:
            return []
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - (window_minutes * 60 * 1000)
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._logs.filter_log_events(
                    logGroupName=log_group,
                    startTime=start_ms,
                    endTime=end_ms,
                    filterPattern=filter_pattern,
                    limit=max_lines,
                ),
            )
            return [evt.get("message", "") for evt in resp.get("events", [])]
        except (BotoCoreError, ClientError, Exception) as exc:
            logger.debug(f"[{self._agent_name}] _get_recent_log_lines({log_group}): {exc}")
            return []

    # ── Lifecycle stubs ────────────────────────────────────────────────────────

    async def on_start(self) -> None:
        if not _BOTO3_AVAILABLE:
            logger.warning(
                f"[{self._agent_name}] boto3 is not installed. "
                "Run: pip install 'ppa[aws]'  — agent will produce no events."
            )
        else:
            logger.info(f"[{self._agent_name}] Starting in region={self._region}")

    async def on_stop(self) -> None:
        logger.info(f"[{self._agent_name}] Stopped.")
