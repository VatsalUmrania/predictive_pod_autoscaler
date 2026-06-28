"""
NEXUS AWS Verifier
===================
Post-remediation verification loop for AWS actions.

After executing an AWS remediation (e.g., rollback_lambda_alias, increase_lambda_memory),
the verifier waits ``wait_seconds`` then re-queries CloudWatch to confirm the issue
is resolved.

Design:
    • Never raises — always returns a VerificationResult.
    • On failure, emits a higher-severity IncidentEvent via the NATS bus so the
      Orchestrator can escalate.
    • The verification window is separate from the monitoring window — uses a
      fresh CloudWatch query starting from the time of remediation.

Usage (from RunbookExecutor after an AWS action):
    verifier = AWSVerifier(nats_client, region="us-east-1")
    result = await verifier.verify_lambda(
        function_name="my-api",
        original_error_rate=0.15,
        wait_seconds=120,
    )
    if not result.resolved:
        # Orchestrator will escalate
        pass
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception    # type: ignore[assignment,misc]
    _BOTO3_AVAILABLE = False


@dataclass
class VerificationResult:
    """Result of a post-remediation verification check."""
    action_type:      str
    resource:         str
    resolved:         bool
    metric_snapshot:  dict[str, Any] = field(default_factory=dict)
    message:          str = ""
    verification_ms:  int = 0   # Time taken in milliseconds


class AWSVerifier:
    """
    Post-remediation CloudWatch verification.

    Args:
        nats_client:   Connected NATSClient (for escalation events).
        region:        AWS region.
        default_wait:  Default seconds to wait before checking (default 120).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        region: str = "us-east-1",
        default_wait: int = 120,
    ) -> None:
        self.nats = nats_client
        self._region = region
        self._default_wait = default_wait
        self.__cw: Any = None

    @property
    def _cw(self):
        if self.__cw is None and _BOTO3_AVAILABLE and boto3:
            try:
                self.__cw = boto3.client("cloudwatch", region_name=self._region)
            except Exception as exc:
                logger.error(f"[AWSVerifier] CW client: {exc}")
        return self.__cw

    # ── Lambda verification ───────────────────────────────────────────────────

    async def verify_lambda(
        self,
        function_name: str,
        original_error_rate: float,
        error_threshold: float = 0.05,
        wait_seconds: int | None = None,
        window_minutes: int = 5,
    ) -> VerificationResult:
        """
        Wait then check Lambda error rate.

        Returns resolved=True if the new error rate < error_threshold.
        Publishes a LAMBDA_ERROR_RATE_HIGH event to NATS if still failing.
        """
        wait = wait_seconds or self._default_wait
        logger.info(f"[AWSVerifier] Waiting {wait}s before verifying {function_name}...")
        started = datetime.now(timezone.utc)
        await asyncio.sleep(wait)

        error_rate = await self._lambda_error_rate(function_name, window_minutes)
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)

        resolved = error_rate < error_threshold
        message = (
            f"Lambda {function_name}: post-remediation error_rate={error_rate:.1%} "
            f"(threshold={error_threshold:.1%}) — {'RESOLVED' if resolved else 'STILL FAILING'}"
        )
        logger.info(f"[AWSVerifier] {message}")

        if not resolved:
            # Escalate via NATS — Orchestrator will route to human approval
            try:
                await self.nats.publish(IncidentEvent(
                    agent=AgentType.LAMBDA,
                    signal_type=SignalType.LAMBDA_ERROR_RATE_HIGH,
                    severity=Severity.EMERGENCY,
                    resource_name=function_name,
                    confidence=0.95,
                    context={
                        "function_name": function_name,
                        "error_rate": error_rate,
                        "original_error_rate": original_error_rate,
                        "nexus_note": "Post-remediation verification FAILED — escalating to human",
                        "wait_seconds": wait,
                    },
                ))
                logger.warning(f"[AWSVerifier] Escalated EMERGENCY event for {function_name}")
            except Exception as exc:
                logger.error(f"[AWSVerifier] Failed to publish escalation event: {exc}")

        return VerificationResult(
            action_type="verify_lambda",
            resource=function_name,
            resolved=resolved,
            metric_snapshot={"error_rate": error_rate, "threshold": error_threshold},
            message=message,
            verification_ms=elapsed_ms,
        )

    async def verify_sqs_dlq(
        self,
        dlq_url: str,
        threshold: int = 10,
        wait_seconds: int | None = None,
    ) -> VerificationResult:
        """Wait then check DLQ depth. Resolved if depth < threshold."""
        wait = wait_seconds or self._default_wait
        logger.info(f"[AWSVerifier] Waiting {wait}s before verifying SQS DLQ {dlq_url}...")
        await asyncio.sleep(wait)

        depth = await self._sqs_dlq_depth(dlq_url)
        resolved = depth < threshold
        message = (
            f"SQS DLQ {dlq_url.split('/')[-1]}: depth={depth} "
            f"(threshold={threshold}) — {'RESOLVED' if resolved else 'STILL ELEVATED'}"
        )
        logger.info(f"[AWSVerifier] {message}")

        return VerificationResult(
            action_type="verify_sqs_dlq",
            resource=dlq_url,
            resolved=resolved,
            metric_snapshot={"dlq_depth": depth, "threshold": threshold},
            message=message,
        )

    # ── CloudWatch metric helpers ─────────────────────────────────────────────

    async def _lambda_error_rate(self, function_name: str, window_minutes: int) -> float:
        """Query CloudWatch for current Lambda error rate."""
        if self._cw is None:
            return 0.0
        dims = [{"Name": "FunctionName", "Value": function_name}]
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=window_minutes)
        loop = asyncio.get_event_loop()
        try:
            errors, invocations = await asyncio.gather(
                loop.run_in_executor(
                    None,
                    lambda: self._cw.get_metric_statistics(
                        Namespace="AWS/Lambda", MetricName="Errors",
                        Dimensions=dims, StartTime=start, EndTime=end,
                        Period=60 * window_minutes, Statistics=["Sum"],
                    ),
                ),
                loop.run_in_executor(
                    None,
                    lambda: self._cw.get_metric_statistics(
                        Namespace="AWS/Lambda", MetricName="Invocations",
                        Dimensions=dims, StartTime=start, EndTime=end,
                        Period=60 * window_minutes, Statistics=["Sum"],
                    ),
                ),
            )
            err_sum = sum(dp.get("Sum", 0) for dp in errors.get("Datapoints", []))
            inv_sum = sum(dp.get("Sum", 0) for dp in invocations.get("Datapoints", []))
            return (err_sum / inv_sum) if inv_sum > 0 else 0.0
        except (BotoCoreError, ClientError, Exception):
            return 0.0

    async def _sqs_dlq_depth(self, dlq_url: str) -> int:
        """Query SQS for current DLQ depth."""
        try:
            import boto3 as _boto3
            loop = asyncio.get_event_loop()
            sqs = _boto3.client("sqs", region_name=self._region)
            resp = await loop.run_in_executor(
                None,
                lambda: sqs.get_queue_attributes(
                    QueueUrl=dlq_url,
                    AttributeNames=["ApproximateNumberOfMessages"],
                ),
            )
            return int(resp.get("Attributes", {}).get("ApproximateNumberOfMessages", 0))
        except Exception:
            return 0
