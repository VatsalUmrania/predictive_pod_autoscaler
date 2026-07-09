"""
Log Analyst Agent
==================
Calls Amazon Bedrock (Claude) to analyze CloudWatch log lines and explain
what went wrong, why, and how to fix it.

Uses boto3's bedrock-runtime client — no external API key needed on Lambda,
auth is handled by the Lambda execution role's IAM permissions.

Required IAM permission on the Lambda role:
    bedrock:InvokeModel  on  arn:aws:bedrock:*::foundation-model/anthropic.claude-*

This is intentionally simpler than the full diagnosis_agent — it receives
raw log lines instead of a fully assembled context dict. Its only job is:

  log lines → Claude (Bedrock) → {severity, root_cause, what_it_means, how_to_fix}

Every error gets notified. The LLM always gives reasoning, even if
the issue is "this is an IAM permission problem" or "queue doesn't exist".
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

from aws.config import AgentConfig

logger = logging.getLogger(__name__)


_SAFE_DEFAULT: dict[str, Any] = {
    "severity":       "Unknown",
    "root_cause":     "Unable to analyze — Bedrock unavailable",
    "what_it_means":  "The AI analysis could not complete. Review the raw logs manually.",
    "how_to_fix":     "Check the raw error lines above.",
    "error_types":    [],
    "is_infra_issue": False,
}

_SYSTEM_PROMPT = """\
You are a Senior AWS DevOps Engineer. You are reviewing error logs from an AWS Lambda function.

Your job is to explain:
1. What went wrong (in plain English, no jargon)
2. Why it happened (root cause)
3. How to fix it

Be SPECIFIC. If you see "AccessDeniedException", say exactly which IAM permission is missing.
If you see "QueueDoesNotExist", say the SQS_QUEUE_URL environment variable is wrong or queue wasn't created.
If you see "Task timed out", say the Lambda timeout setting is too low.

Respond ONLY with valid JSON, no markdown fences:
{
  "severity": "Critical | High | Medium | Low",
  "root_cause": "one sentence — what exactly failed",
  "what_it_means": "1-2 sentences — impact on users/system",
  "how_to_fix": "numbered list of concrete steps to fix this",
  "error_types": ["AccessDeniedException", ...],
  "is_infra_issue": true/false
}

Severity guide:
- Critical: data loss, security breach, complete service outage
- High: user-facing errors, service degraded
- Medium: internal errors, retried successfully
- Low: warnings, expected test failures
"""


def analyze_logs(
    function_name: str,
    log_group: str,
    log_lines: list[str],
    region: str = "us-east-1",
    cfg: AgentConfig | None = None,
) -> dict[str, Any]:
    """
    Send log lines to Claude via Bedrock for analysis.

    Args:
        function_name: Lambda function name
        log_group:     CloudWatch log group (e.g. /aws/lambda/my-fn)
        log_lines:     List of raw log message strings
        region:        AWS region (used for Bedrock client)
        cfg:           AgentConfig (loaded from env if None)

    Returns:
        Analysis dict: {severity, root_cause, what_it_means, how_to_fix, ...}
    """
    cfg = cfg or AgentConfig.load()
    user_message = _build_prompt(function_name, log_group, log_lines, region)

    logger.info(f"[LogAnalyst] Analyzing {len(log_lines)} log lines from {log_group}")

    # Build boto3 Bedrock client — on Lambda, credentials come from the IAM role
    bedrock_kwargs: dict[str, Any] = {"region_name": region}
    if cfg.bedrock_api_key:
        # Bearer token auth (local dev / CI without a full IAM role)
        bedrock_kwargs["aws_bearer_token"] = cfg.bedrock_api_key
    elif cfg.aws_access_key_id and cfg.aws_secret_access_key:
        bedrock_kwargs["aws_access_key_id"]     = cfg.aws_access_key_id
        bedrock_kwargs["aws_secret_access_key"] = cfg.aws_secret_access_key

    bedrock = boto3.client("bedrock-runtime", **bedrock_kwargs)

    try:
        # converse() is model-agnostic — works with Claude, Llama, Mistral, Titan, etc.
        # No need to know the provider-specific request body schema.
        response = bedrock.converse(
            modelId=cfg.bedrock_model,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": 1024},
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        logger.debug(f"[LogAnalyst] Bedrock response: {raw[:300]}")
        return _parse(raw)

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "UnauthorizedException"):
            logger.error(
                "[LogAnalyst] Bedrock access denied — add bedrock:InvokeModel "
                f"to the Lambda IAM role. Model: {cfg.bedrock_model}"
            )
            return {
                **_SAFE_DEFAULT,
                "root_cause": (
                    f"Bedrock access denied (IAM). "
                    f"Add bedrock:InvokeModel permission for model {cfg.bedrock_model}."
                ),
            }
        if code == "ValidationException":
            logger.error(f"[LogAnalyst] Bedrock ValidationException: {exc}")
            return {**_SAFE_DEFAULT, "root_cause": f"Bedrock model validation error: {exc}"}
        logger.error(f"[LogAnalyst] Bedrock ClientError ({code}): {exc}")
        return {**_SAFE_DEFAULT, "root_cause": f"Bedrock error ({code}): {exc}"}

    except Exception as exc:
        logger.error(f"[LogAnalyst] Bedrock call failed: {exc}")
        return {**_SAFE_DEFAULT, "root_cause": f"Analysis failed: {exc}"}


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(
    function_name: str,
    log_group: str,
    log_lines: list[str],
    region: str,
) -> str:
    lines = [
        f"## Lambda Function: `{function_name}`",
        f"## Log Group: `{log_group}`",
        f"## Region: `{region}`",
        f"## Error Count: {len(log_lines)}",
        "",
        "## Raw Error Log Lines:",
        "```",
    ]
    # Send at most 50 lines to avoid hitting token limits
    for line in log_lines[:50]:
        lines.append(line[:500])   # Truncate very long lines
    lines.append("```")
    lines.append("")
    lines.append(
        "Analyze these errors. What went wrong? Why? How to fix it? "
        "Be specific about IAM permissions, missing resources, configuration, or code bugs."
    )
    return "\n".join(lines)


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse(raw: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        logger.warning(f"[LogAnalyst] JSON parse failed: {exc} — raw: {raw[:200]}")
        return _SAFE_DEFAULT.copy()

    data.setdefault("severity", "Unknown")
    data.setdefault("root_cause", "Unknown")
    data.setdefault("what_it_means", "")
    data.setdefault("how_to_fix", "")
    data.setdefault("error_types", [])
    data.setdefault("is_infra_issue", False)
    return data
