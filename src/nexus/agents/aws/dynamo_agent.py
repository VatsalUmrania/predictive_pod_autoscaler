"""
NEXUS DynamoDB Agent
=====================
Monitors AWS DynamoDB tables for throttling, system errors, and
consumed capacity approaching provisioned limits.

Published IncidentEvents:
    DYNAMO_THROTTLE_READ     — ReadThrottleEvents > threshold
    DYNAMO_THROTTLE_WRITE    — WriteThrottleEvents > threshold
    DYNAMO_SYSTEM_ERROR      — SystemErrors metric > 0
    DYNAMO_CONSUMED_RCU_HIGH — consumed RCU > 80% of provisioned
"""

from __future__ import annotations

import asyncio
import logging

from nexus.agents.aws.base_aws_agent import BaseAWSAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import (
    AgentType,
    DynamoThrottleContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)
_CW_NS = "AWS/DynamoDB"


class DynamoDBAgent(BaseAWSAgent):
    """Monitors DynamoDB tables for throttling and capacity issues."""

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.DYNAMODB,
            config=config,
        )
        self._known_tables: list[str] = []

    async def sense(self) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []

        if not self._known_tables:
            self._known_tables = await self._list_tables()

        tasks = [self._check_table(table) for table in self._known_tables]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                events.extend(r)

        if self._total_events_published % 10 == 9:
            self._known_tables = []
        return events

    async def _list_tables(self) -> list[str]:
        if self._dynamodb is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            tables = []
            resp = await loop.run_in_executor(None, lambda: self._dynamodb.list_tables(Limit=100))
            while True:
                tables.extend(resp.get("TableNames", []))
                last = resp.get("LastEvaluatedTableName")
                if not last:
                    break
                resp = await loop.run_in_executor(
                    None,
                    lambda l=last: self._dynamodb.list_tables(Limit=100, ExclusiveStartTableName=l),
                )
            logger.info(f"[DynamoDBAgent] Monitoring {len(tables)} DynamoDB table(s)")
            return tables
        except Exception as exc:
            logger.debug(f"[DynamoDBAgent] list_tables: {exc}")
            return []

    async def _check_table(self, table_name: str) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []
        dims = [{"Name": "TableName", "Value": table_name}]
        window = self.cfg.cw_window_minutes

        # Parallel metric queries
        (throttle_read, throttle_write, system_errors,
         consumed_rcu, consumed_wcu) = await asyncio.gather(
            self._get_metric_sum(_CW_NS, "ReadThrottleEvents", dims, window),
            self._get_metric_sum(_CW_NS, "WriteThrottleEvents", dims, window),
            self._get_metric_sum(_CW_NS, "SystemErrors", dims, window),
            self._get_metric_average(_CW_NS, "ConsumedReadCapacityUnits", dims, window),
            self._get_metric_average(_CW_NS, "ConsumedWriteCapacityUnits", dims, window),
        )

        # Fetch table description for provisioned capacity
        table_info = await self._describe_table(table_name)
        provisioned_rcu = table_info.get("ProvisionedThroughput", {}).get("ReadCapacityUnits")
        provisioned_wcu = table_info.get("ProvisionedThroughput", {}).get("WriteCapacityUnits")
        capacity_mode = "PAY_PER_REQUEST" if table_info.get("BillingModeSummary", {}).get(
            "BillingMode") == "PAY_PER_REQUEST" else "PROVISIONED"
        table_status = table_info.get("TableStatus")
        gsi_names = [gsi.get("IndexName", "") for gsi in table_info.get("GlobalSecondaryIndexes", [])]

        ctx = DynamoThrottleContext(
            table_name=table_name,
            region=self._region,
            throttle_read=int(throttle_read),
            throttle_write=int(throttle_write),
            system_errors=int(system_errors),
            capacity_mode=capacity_mode,
            provisioned_rcu=float(provisioned_rcu) if provisioned_rcu else None,
            provisioned_wcu=float(provisioned_wcu) if provisioned_wcu else None,
            consumed_rcu=consumed_rcu if consumed_rcu > 0 else None,
            consumed_wcu=consumed_wcu if consumed_wcu > 0 else None,
            table_status=table_status,
            gsi_names=gsi_names,
        )

        threshold = self.cfg.dynamo_throttle_threshold

        if throttle_read >= threshold:
            events.append(IncidentEvent(
                agent=AgentType.DYNAMODB,
                signal_type=SignalType.DYNAMO_THROTTLE_READ,
                severity=Severity.WARNING,
                resource_name=table_name,
                confidence=0.80,
                context=ctx.model_dump(),
            ))
            logger.warning(f"[DynamoDBAgent] {table_name}: read_throttles={int(throttle_read)}")

        if throttle_write >= threshold:
            events.append(IncidentEvent(
                agent=AgentType.DYNAMODB,
                signal_type=SignalType.DYNAMO_THROTTLE_WRITE,
                severity=Severity.WARNING,
                resource_name=table_name,
                confidence=0.80,
                context=ctx.model_dump(),
            ))
            logger.warning(f"[DynamoDBAgent] {table_name}: write_throttles={int(throttle_write)}")

        if system_errors > 0:
            events.append(IncidentEvent(
                agent=AgentType.DYNAMODB,
                signal_type=SignalType.DYNAMO_SYSTEM_ERROR,
                severity=Severity.CRITICAL,
                resource_name=table_name,
                confidence=0.90,
                context=ctx.model_dump(),
            ))

        return events

    async def _describe_table(self, table_name: str) -> dict:
        """Fetch table description. Returns {} on error."""
        if self._dynamodb is None:
            return {}
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self._dynamodb.describe_table(TableName=table_name),
            )
            return resp.get("Table", {})
        except Exception:
            return {}
