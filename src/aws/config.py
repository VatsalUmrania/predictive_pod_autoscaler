"""
AWS AI DevOps Agent — Configuration
=====================================
All settings come from environment variables. No secrets hard-coded.

Required environment variables:
    ANTHROPIC_API_KEY           Claude Sonnet API key

Optional environment variables:
    AWS_DEFAULT_REGION          (default: us-east-1)
    AWS_ACCESS_KEY_ID           AWS credentials (or use IAM role — recommended on Lambda)
    AWS_SECRET_ACCESS_KEY

    AGENT_LAMBDA_PREFIX         Only monitor functions starting with this prefix (default: all)
    AGENT_ERROR_THRESHOLD       Lambda error rate threshold 0.0-1.0 (default: 0.05 = 5%)
    AGENT_THROTTLE_THRESHOLD    Lambda throttle count before alert (default: 10)
    AGENT_DLQ_THRESHOLD         SQS DLQ depth before alert (default: 10)
    AGENT_DYNAMO_THRESHOLD      DynamoDB throttle count before alert (default: 5)
    AGENT_CW_WINDOW_MINUTES     CloudWatch lookback window in minutes (default: 5)

    AGENT_CONFIDENCE_THRESHOLD  Min confidence for autonomous action (default: 0.85)
    AGENT_DRY_RUN               Set to "true" to skip real AWS actions (default: false)

    SLACK_WEBHOOK_URL           Slack incoming webhook for notifications
    SLACK_APPROVAL_WEBHOOK      Optional separate webhook for approval requests

    AGENT_VERIFY_WAIT_SECONDS   Seconds to wait before post-remediation check (default: 120)
    AGENT_MAX_RETRIES           Max execute→verify retry cycles (default: 2)
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentConfig(BaseSettings):
    """
    Configuration for the AWS AI DevOps Agent.
    All fields are loaded from environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""          # loaded from ANTHROPIC_API_KEY (no prefix)
    claude_model: str = "claude-sonnet-4-5"

    # ── AWS ──────────────────────────────────────────────────────────────────
    aws_region: str = "us-east-1"       # loaded from AWS_DEFAULT_REGION (no prefix)

    # ── Monitoring thresholds ─────────────────────────────────────────────────
    lambda_prefix: str = ""             # "" = monitor all functions
    error_threshold: float = 0.05       # 5% error rate
    throttle_threshold: int = 10        # throttle event count
    dlq_threshold: int = 10             # DLQ depth
    dynamo_threshold: int = 5           # DynamoDB throttle count
    cw_window_minutes: int = 5          # CloudWatch metric window

    # ── Governance ────────────────────────────────────────────────────────────
    confidence_threshold: float = 0.85  # below this → human approval required
    dry_run: bool = False               # True = log actions but don't call AWS

    # ── Verification ─────────────────────────────────────────────────────────
    verify_wait_seconds: int = 120      # wait before post-remediation check
    max_retries: int = 2                # max execute→verify cycles

    # ── Notifications ─────────────────────────────────────────────────────────
    slack_webhook_url: str = ""         # Slack incoming webhook URL
    slack_approval_webhook: str = ""    # Optional separate approval channel


    @classmethod
    def load(cls) -> "AgentConfig":
        """Load config from environment. Call once at Lambda cold start."""
        import os
        # Fields without the AGENT_ prefix need manual handling
        instance = cls(
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            aws_region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        return instance

    def is_lambda_monitored(self, function_name: str) -> bool:
        """Return True if this Lambda function should be monitored."""
        if self.lambda_prefix:
            return function_name.startswith(self.lambda_prefix)
        return True
