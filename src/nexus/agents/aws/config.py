"""
NEXUS AWS Agent Configuration
==============================
Centralised configuration for all AWS serverless monitoring agents.
All values come from environment variables — no secrets are hard-coded.

Environment variables:
    AWS_DEFAULT_REGION          AWS region to monitor (default: us-east-1)
    AWS_ACCESS_KEY_ID           AWS credentials (or use IAM role / ~/.aws/credentials)
    AWS_SECRET_ACCESS_KEY
    AWS_SESSION_TOKEN           Optional — for temporary credentials

    NEXUS_AWS_ENABLED           Set to "true" to activate AWS agents (default: false)
    NEXUS_LAMBDA_PREFIX         Only monitor functions whose name starts with this prefix
                                (default: "" = monitor all)
    NEXUS_LAMBDA_TAG_KEY        Only monitor functions with this tag key (optional)
    NEXUS_LAMBDA_TAG_VALUE      Only monitor functions with this tag value (optional)
    NEXUS_LAMBDA_ERROR_THRESHOLD  Error-rate threshold 0.0–1.0 (default: 0.05 = 5%)
    NEXUS_LAMBDA_THROTTLE_THRESHOLD Throttle count before alert (default: 10)
    NEXUS_SQS_DLQ_THRESHOLD     DLQ message count before alert (default: 10)
    NEXUS_SQS_QUEUE_DEPTH_THRESHOLD  Source queue depth before alert (default: 1000)
    NEXUS_DYNAMO_THROTTLE_THRESHOLD  Throttle count before alert (default: 5)
    NEXUS_APIGW_5XX_THRESHOLD   5XX error rate 0.0–1.0 before alert (default: 0.05)
    NEXUS_APIGW_LATENCY_THRESHOLD_MS  P99 latency in ms before alert (default: 3000)
    NEXUS_AWS_POLL_INTERVAL     CloudWatch polling interval in seconds (default: 60)
    NEXUS_AWS_CW_WINDOW_MINUTES CloudWatch metric window in minutes (default: 5)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class AWSConfig:
    """
    Immutable configuration object for all AWS agents.
    Build once via ``AWSConfig.from_env()`` and share across agents.
    """

    # ── AWS SDK ───────────────────────────────────────────────────────────────
    region: str = "us-east-1"

    # ── Agent activation ──────────────────────────────────────────────────────
    enabled: bool = False

    # ── Lambda ────────────────────────────────────────────────────────────────
    lambda_prefix: str = ""            # "" = monitor all functions
    lambda_tag_key: str | None = None  # Only monitor functions with this tag key
    lambda_tag_value: str | None = None
    lambda_error_threshold: float = 0.05   # 5% error rate
    lambda_throttle_threshold: int = 10    # throttle event count
    lambda_max_log_lines: int = 200        # lines to fetch per function on incident

    # ── SQS ───────────────────────────────────────────────────────────────────
    sqs_dlq_threshold: int = 10            # DLQ depth before alert
    sqs_queue_depth_threshold: int = 1000  # source queue depth
    sqs_max_sample_messages: int = 5       # message bodies to include in context

    # ── DynamoDB ──────────────────────────────────────────────────────────────
    dynamo_throttle_threshold: int = 5     # throttle event count

    # ── API Gateway ───────────────────────────────────────────────────────────
    apigw_5xx_threshold: float = 0.05      # 5% 5XX rate
    apigw_latency_threshold_ms: float = 3000.0  # 3s P99 latency

    # ── Polling ───────────────────────────────────────────────────────────────
    poll_interval_seconds: float = 60.0    # How often to query CloudWatch
    cw_window_minutes: int = 5             # CloudWatch metric window

    # ── Monitoring targets (populated at runtime) ─────────────────────────────
    extra_lambda_functions: list[str] = field(default_factory=list)  # explicit function names

    @classmethod
    def from_env(cls) -> "AWSConfig":
        """Build AWSConfig from environment variables."""
        return cls(
            region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            enabled=os.getenv("NEXUS_AWS_ENABLED", "false").lower() in ("1", "true", "yes"),
            lambda_prefix=os.getenv("NEXUS_LAMBDA_PREFIX", ""),
            lambda_tag_key=os.getenv("NEXUS_LAMBDA_TAG_KEY") or None,
            lambda_tag_value=os.getenv("NEXUS_LAMBDA_TAG_VALUE") or None,
            lambda_error_threshold=float(os.getenv("NEXUS_LAMBDA_ERROR_THRESHOLD", "0.05")),
            lambda_throttle_threshold=int(os.getenv("NEXUS_LAMBDA_THROTTLE_THRESHOLD", "10")),
            sqs_dlq_threshold=int(os.getenv("NEXUS_SQS_DLQ_THRESHOLD", "10")),
            sqs_queue_depth_threshold=int(os.getenv("NEXUS_SQS_QUEUE_DEPTH_THRESHOLD", "1000")),
            dynamo_throttle_threshold=int(os.getenv("NEXUS_DYNAMO_THROTTLE_THRESHOLD", "5")),
            apigw_5xx_threshold=float(os.getenv("NEXUS_APIGW_5XX_THRESHOLD", "0.05")),
            apigw_latency_threshold_ms=float(os.getenv("NEXUS_APIGW_LATENCY_THRESHOLD_MS", "3000")),
            poll_interval_seconds=float(os.getenv("NEXUS_AWS_POLL_INTERVAL", "60")),
            cw_window_minutes=int(os.getenv("NEXUS_AWS_CW_WINDOW_MINUTES", "5")),
        )

    def is_lambda_monitored(self, function_name: str) -> bool:
        """Return True if this Lambda function should be monitored."""
        if self.lambda_prefix and not function_name.startswith(self.lambda_prefix):
            return False
        return True
