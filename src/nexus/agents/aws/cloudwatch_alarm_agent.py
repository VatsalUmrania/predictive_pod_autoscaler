"""
NEXUS CloudWatch Alarm Agent
=============================
Catch-all agent that watches all CloudWatch Alarms in ALARM state.
This covers any metric not explicitly handled by the specific domain agents
(Lambda, SQS, DynamoDB, API GW) — e.g., custom application metrics,
Step Functions failures, Kinesis/EventBridge alarms, etc.

Published IncidentEvents:
    AWS_ALARM_FIRED  — any CloudWatch Alarm that enters ALARM state
"""

from __future__ import annotations

import asyncio
import logging

from nexus.agents.aws.base_aws_agent import BaseAWSAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import (
    AgentType,
    CloudWatchAlarmContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

# Alarms we already handle via specific agents (avoid duplicate events)
_HANDLED_NAMESPACES = {
    "AWS/Lambda",
    "AWS/ApiGateway",
    "AWS/SQS",
    "AWS/DynamoDB",
}


class CloudWatchAlarmAgent(BaseAWSAgent):
    """
    Catch-all CloudWatch Alarm agent.
    Scans all alarms in ALARM state and emits events for namespaces not
    covered by the dedicated domain agents.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.CLOUDWATCH,
            config=config,
        )

    async def sense(self) -> list[IncidentEvent]:
        if self._cw is None:
            return []
        events: list[IncidentEvent] = []

        alarms = await self._list_alarming()
        for alarm in alarms:
            namespace = alarm.get("Namespace", "")
            # Skip alarms already covered by specific agents
            if namespace in _HANDLED_NAMESPACES:
                continue

            ctx = CloudWatchAlarmContext(
                alarm_name=alarm.get("AlarmName", ""),
                alarm_arn=alarm.get("AlarmArn"),
                metric_name=alarm.get("MetricName"),
                namespace=namespace,
                state=alarm.get("StateValue", "ALARM"),
                state_reason=alarm.get("StateReason"),
                threshold=alarm.get("Threshold"),
                comparison_operator=alarm.get("ComparisonOperator"),
                evaluation_periods=alarm.get("EvaluationPeriods"),
                period_seconds=alarm.get("Period"),
                dimensions={
                    d.get("Name", ""): d.get("Value", "")
                    for d in alarm.get("Dimensions", [])
                },
            )

            severity = Severity.WARNING
            state_reason = (alarm.get("StateReason") or "").lower()
            if "critical" in state_reason or alarm.get("AlarmDescription", "").lower().startswith("critical"):
                severity = Severity.CRITICAL

            events.append(IncidentEvent(
                agent=AgentType.CLOUDWATCH,
                signal_type=SignalType.AWS_ALARM_FIRED,
                severity=severity,
                resource_name=alarm.get("AlarmName", ""),
                confidence=0.70,
                context=ctx.model_dump(),
            ))
            logger.warning(f"[CloudWatchAlarmAgent] Alarm in ALARM state: {alarm.get('AlarmName')}")

        return events

    async def _list_alarming(self) -> list[dict]:
        """Fetch all CloudWatch alarms currently in ALARM state."""
        if self._cw is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            alarms = []
            resp = await loop.run_in_executor(
                None,
                lambda: self._cw.describe_alarms(
                    StateValue="ALARM",
                    MaxRecords=100,
                ),
            )
            while True:
                alarms.extend(resp.get("MetricAlarms", []))
                token = resp.get("NextToken")
                if not token:
                    break
                resp = await loop.run_in_executor(
                    None,
                    lambda t=token: self._cw.describe_alarms(
                        StateValue="ALARM",
                        MaxRecords=100,
                        NextToken=t,
                    ),
                )
            return alarms
        except Exception as exc:
            logger.debug(f"[CloudWatchAlarmAgent] describe_alarms: {exc}")
            return []
