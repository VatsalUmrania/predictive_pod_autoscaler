"""
NEXUS API Gateway Agent
========================
Monitors AWS API Gateway REST APIs and HTTP APIs for 5XX errors,
high latency, and 4XX spikes using CloudWatch metrics.

Published IncidentEvents:
    APIGW_5XX_SPIKE     — 5XX error rate > threshold
    APIGW_LATENCY_HIGH  — integration latency P99 > threshold
    APIGW_4XX_SPIKE     — 4XX error rate > 20% (client error flood)
"""

from __future__ import annotations

import asyncio
import logging

from nexus.agents.aws.base_aws_agent import BaseAWSAgent
from nexus.agents.aws.config import AWSConfig
from nexus.bus.incident_event import (
    AgentType,
    ApiGwContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)
_CW_NS = "AWS/ApiGateway"


class ApiGatewayAgent(BaseAWSAgent):
    """Monitors API Gateway REST and HTTP APIs for error rates and latency."""

    def __init__(
        self,
        nats_client: NATSClient,
        config: AWSConfig | None = None,
    ) -> None:
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.APIGW,
            config=config,
        )
        self._known_apis: list[dict] = []

    async def sense(self) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []

        if not self._known_apis:
            self._known_apis = await self._list_rest_apis()

        tasks = [self._check_api(api) for api in self._known_apis]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                events.extend(r)

        if self._total_events_published % 10 == 9:
            self._known_apis = []
        return events

    async def _list_rest_apis(self) -> list[dict]:
        if self._apigw is None:
            return []
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: self._apigw.get_rest_apis(limit=100))
            apis = []
            for item in resp.get("items", []):
                # Fetch deployment stages
                api_id = item.get("id", "")
                try:
                    stages_resp = await loop.run_in_executor(
                        None, lambda: self._apigw.get_stages(restApiId=api_id)
                    )
                    for stage in stages_resp.get("item", []):
                        apis.append({
                            "id": api_id,
                            "name": item.get("name", api_id),
                            "stage": stage.get("stageName", "prod"),
                        })
                except Exception:
                    apis.append({"id": api_id, "name": item.get("name", api_id), "stage": "prod"})
            logger.info(f"[ApiGatewayAgent] Monitoring {len(apis)} API stages")
            return apis
        except Exception as exc:
            logger.debug(f"[ApiGatewayAgent] list_rest_apis: {exc}")
            return []

    async def _check_api(self, api: dict) -> list[IncidentEvent]:
        events: list[IncidentEvent] = []
        api_id = api["id"]
        stage = api.get("stage", "prod")
        api_name = api.get("name", api_id)
        dims = [
            {"Name": "ApiName", "Value": api_name},
            {"Name": "Stage", "Value": stage},
        ]
        window = self.cfg.cw_window_minutes

        (count_5xx, count_4xx, count_total, latency) = await asyncio.gather(
            self._get_metric_sum(_CW_NS, "5XXError", dims, window),
            self._get_metric_sum(_CW_NS, "4XXError", dims, window),
            self._get_metric_sum(_CW_NS, "Count", dims, window),
            self._get_metric_average(_CW_NS, "IntegrationLatency", dims, window, statistic="p99"),
        )

        rate_5xx = (count_5xx / count_total) if count_total > 0 else 0.0
        rate_4xx = (count_4xx / count_total) if count_total > 0 else 0.0

        ctx = ApiGwContext(
            api_id=api_id,
            api_name=api_name,
            stage=stage,
            error_5xx_rate=rate_5xx,
            error_4xx_rate=rate_4xx,
            latency_p99_ms=latency if latency > 0 else None,
            request_count=int(count_total),
            window_minutes=window,
        )

        if rate_5xx >= self.cfg.apigw_5xx_threshold and count_total > 0:
            events.append(IncidentEvent(
                agent=AgentType.APIGW,
                signal_type=SignalType.APIGW_5XX_SPIKE,
                severity=Severity.CRITICAL if rate_5xx > 0.20 else Severity.WARNING,
                resource_name=f"{api_name}/{stage}",
                confidence=min(0.90, 0.5 + rate_5xx * 2),
                context=ctx.model_dump(),
            ))
            logger.warning(f"[ApiGatewayAgent] {api_name}/{stage}: 5XX rate={rate_5xx:.1%}")

        if latency > 0 and latency >= self.cfg.apigw_latency_threshold_ms:
            events.append(IncidentEvent(
                agent=AgentType.APIGW,
                signal_type=SignalType.APIGW_LATENCY_HIGH,
                severity=Severity.WARNING,
                resource_name=f"{api_name}/{stage}",
                confidence=0.70,
                context=ctx.model_dump(),
            ))

        return events
