"""
AWS AI DevOps Agent — Configuration
=====================================
All settings come from environment variables. No secrets hard-coded.

Auth options (pick ONE — in priority order):

  1. Bedrock API Key (simplest for local dev / CI):
       AWS_BEARER_TOKEN_BEDROCK=<your Bedrock API key>
     The SDK also accepts this as BEDROCK_API_KEY for convenience.

  2. IAM credentials (recommended for Lambda / ECS with roles):
       AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY  (local dev)
       IAM role attached to Lambda/ECS             (production, no keys needed)

Optional environment variables:
    DEFAULT_REGION              (default: us-east-1)
    AWS_ACCESS_KEY_ID           AWS credentials (or use IAM role — recommended on Lambda)
    AWS_SECRET_ACCESS_KEY
    AGENT_CLAUDE_MODEL          Bedrock model ID (default: global.anthropic.claude-sonnet-4-6)

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

    # ── LLM (Amazon Bedrock) ─────────────────────────────────────────────────
    # Option 1 — Bedrock API Key (Bearer token, no IAM needed).
    #   Set AWS_BEARER_TOKEN_BEDROCK in your environment.
    # Option 2 — IAM credentials / role (see AWS section below).
    bedrock_api_key: str = ""                 # loaded from AWS_BEARER_TOKEN_BEDROCK
    claude_model: str = "global.anthropic.claude-sonnet-4-6"

    # ── AWS ──────────────────────────────────────────────────────────────────
    aws_region: str = "us-east-1"             # loaded from DEFAULT_REGION (no prefix)
    aws_access_key_id: str = ""               # loaded from AWS_ACCESS_KEY_ID (no prefix)
    aws_secret_access_key: str = ""           # loaded from AWS_SECRET_ACCESS_KEY (no prefix)

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
        # Fields without the AGENT_ prefix need manual handling.
        # AWS_BEARER_TOKEN_BEDROCK is the canonical env var; fall back to
        # BEDROCK_API_KEY as an alias for convenience.
        bedrock_api_key = (
            os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
            or os.getenv("BEDROCK_API_KEY", "")
        )
        instance = cls(
            bedrock_api_key=bedrock_api_key,
            aws_region=os.getenv("DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        )
        return instance

    def is_lambda_monitored(self, function_name: str) -> bool:
        """Return True if this Lambda function should be monitored."""
        if self.lambda_prefix:
            return function_name.startswith(self.lambda_prefix)
        return True
