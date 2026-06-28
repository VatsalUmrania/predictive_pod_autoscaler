"""
NEXUS AWS Agent Manager
========================
Lifecycle manager for all 5 AWS serverless domain agents.
Activated only when NEXUS_AWS_ENABLED=true.

Managed agents:
    LambdaAgent           — Lambda error rate, throttles, timeouts, OOM
    ApiGatewayAgent       — API GW 5XX/4XX spikes and latency
    SqsAgent              — SQS DLQ depth and queue depth
    DynamoDBAgent         — DynamoDB throttling and system errors
    CloudWatchAlarmAgent  — Catch-all CloudWatch alarms in ALARM state
"""

from __future__ import annotations

import asyncio
import logging

from nexus.agents.aws.apigw_agent import ApiGatewayAgent
from nexus.agents.aws.cloudwatch_alarm_agent import CloudWatchAlarmAgent
from nexus.agents.aws.config import AWSConfig
from nexus.agents.aws.dynamo_agent import DynamoDBAgent
from nexus.agents.aws.lambda_agent import LambdaAgent
from nexus.agents.aws.sqs_agent import SqsAgent
from nexus.agents.base_agent import BaseAgent
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class AWSAgentManager:
    """
    Manages the lifecycle of all 5 AWS serverless monitoring agents.

    All agents share the same NATSClient and AWSConfig.
    Only activated when ``config.enabled`` is True.

    Args:
        nats_client:   Connected NATSClient.
        config:        AWSConfig (reads from env if None).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        self.cfg = config or AWSConfig.from_env()
        self.nats_client = nats_client
        self.agents: list[BaseAgent] = []
        self._tasks: list[asyncio.Task] = []
        self._enabled = self.cfg.enabled

    def _build_agents(self) -> list[BaseAgent]:
        return [
            LambdaAgent(self.nats_client, config=self.cfg),
            ApiGatewayAgent(self.nats_client, config=self.cfg),
            SqsAgent(self.nats_client, config=self.cfg),
            DynamoDBAgent(self.nats_client, config=self.cfg),
            CloudWatchAlarmAgent(self.nats_client, config=self.cfg),
        ]

    async def start(self) -> None:
        """
        Spawn all AWS agent run-loop tasks.
        No-op if NEXUS_AWS_ENABLED is not true.
        """
        if not self._enabled:
            logger.info("[AWSAgentManager] NEXUS_AWS_ENABLED=false — AWS agents not started")
            return

        self.agents = self._build_agents()
        self._tasks = [
            asyncio.create_task(agent.run(), name=f"aws-agent-{agent.__class__.__name__}")
            for agent in self.agents
        ]
        logger.info(f"[AWSAgentManager] Started {len(self._tasks)} AWS agent(s) in region={self.cfg.region}")

    def stop(self) -> None:
        """Signal all AWS agents to stop after the current poll cycle."""
        for agent in self.agents:
            agent.stop()
        for task in self._tasks:
            task.cancel()
        logger.info("[AWSAgentManager] Stopping all AWS agents")

    async def stop_async(self) -> None:
        """Signal stop and await graceful termination."""
        self.stop()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("[AWSAgentManager] All AWS agents stopped")
