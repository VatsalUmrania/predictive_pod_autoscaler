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
    # Action fields (consumed by remediation_agent)
    "action":         "alert_only",
    "params":         {},
    "confidence":     0.0,
    "failure_class":  "unknown",
    # Suggested boto3 commands (consumed by approval flow)
    "suggested_commands": [],
}

_SYSTEM_PROMPT = """\
You are a Senior AWS DevOps Engineer embedded in an autonomous AIOps system.
You receive raw CloudWatch error logs from an AWS Lambda function.

Your job:
1. Explain what went wrong (plain English, no jargon)
2. Identify the root cause
3. Recommend a concrete remediation action
4. Generate specific boto3 API calls to fix the issue (when possible)

Be SPECIFIC:
- "AccessDeniedException" → name the exact IAM action and resource ARN
- "QueueDoesNotExist" → SQS_QUEUE_URL env var missing, wrong, or points to wrong region
- "Task timed out" → Lambda timeout setting is too low
- OOM / MemoryError → Lambda memory_size too low
- "NoSuchBucket", "ResourceNotFoundException", "TableNotFoundException" → resource doesn't exist or wrong region

## Detecting env var / config issues
You are given the Lambda's current env var KEY NAMES (not values) in the prompt.
Use this to detect configuration problems:

- If SQS_QUEUE_URL is missing from env vars → the URL was never set → fix: update_function_configuration
- If DEFAULT_REGION is missing → boto3 will fall back to us-east-1 even if resources are in another region
  → fix: update_function_configuration to add DEFAULT_REGION = <region from prompt header>
- If a "QueueDoesNotExist" error occurs AND DEFAULT_REGION is absent from env vars
  → the queue exists but boto3 is looking in the wrong region
  → fix: add DEFAULT_REGION, do NOT create a new queue
- If "QueueDoesNotExist" AND the queue URL looks correct in env vars
  → the queue was deleted or never created → fix: create_queue + update env var

When you detect a config/env var issue, use action=alert_only and populate suggested_commands
with the exact update_function_configuration call using the real function name and real region
from the prompt header. Never guess the region — it is always given at the top.


## Predefined actions (auto-executed if confidence is high)
| action | when to use | params |
|--------|-------------|--------|
| increase_memory | OOM errors, duration near timeout | {"function_name": str, "memory_mb": int} |
| increase_timeout | Duration exceeded timeout, no OOM | {"function_name": str, "timeout_seconds": int} |
| set_concurrency | Throttle errors from concurrent invocations | {"function_name": str, "reserved_concurrent_executions": int} |
| rollback_alias | Error spike after a recent deploy | {"function_name": str, "alias_name": "live"} |
| replay_dlq | DLQ has messages, root Lambda is now healthy | {"dlq_url": str, "source_queue_url": str} |
| alert_only | Use for IAM issues, missing resources, config problems, or low confidence | {} |

## Suggested commands (human-approved boto3 calls)
For ANY error where you know a specific fix — especially alert_only cases —
populate `suggested_commands` with the exact boto3 API calls needed.
These will be shown to the user for approval before execution.

Allowed boto3 services and methods you can suggest:
- iam: put_role_policy, attach_role_policy
- lambda: update_function_configuration, put_function_concurrency, update_alias, update_function_event_invoke_config
- sqs: create_queue, set_queue_attributes, get_queue_url
- dynamodb: create_table, update_table
- ssm: put_parameter
- cloudwatch: put_metric_alarm

NEVER suggest: delete_*, terminate_*, remove_*, stop_* operations.

Example for AccessDeniedException on dynamodb:PutItem:
"suggested_commands": [
  {
    "service": "iam",
    "method": "put_role_policy",
    "params": {
      "RoleName": "nexus-ai-agent-sample-app-role",
      "PolicyName": "nexus-auto-fix-dynamodb",
      "PolicyDocument": "{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[{\\"Effect\\":\\"Allow\\",\\"Action\\":[\\"dynamodb:PutItem\\",\\"dynamodb:GetItem\\"],\\"Resource\\":\\"*\\"}]}"
    },
    "description": "Add dynamodb:PutItem permission to the Lambda execution role",
    "risk": "low"
  }
]

Example for QueueDoesNotExist when log shows function=nexus-ai-agent-sample-app
and Lambda role is nexus-ai-agent-sample-app-role:
"suggested_commands": [
  {
    "service": "sqs",
    "method": "create_queue",
    "params": {"QueueName": "nexus-ai-agent-sample-app-dlq"},
    "description": "Create the missing SQS dead-letter queue",
    "risk": "low"
  },
  {
    "service": "lambda",
    "method": "update_function_configuration",
    "params": {
      "FunctionName": "nexus-ai-agent-sample-app",
      "Environment": {"Variables": {"DLQ_URL": "https://sqs.ap-south-1.amazonaws.com/804078307169/nexus-ai-agent-sample-app-dlq"}}
    },
    "description": "Update Lambda DLQ_URL env var with the new queue URL",
    "risk": "low"
  }
]

## CRITICAL RULE: NO PLACEHOLDERS
Never use <placeholder> syntax or ${placeholder} syntax in params. The system will REJECT commands containing these formats.
You will be given the actual function name, IAM role name, region, and account ID in the prompt. Use those exact values.
Use those exact values. If you don't have a required value (e.g. the queue URL), omit that command and explain what the user needs to fill in via how_to_fix. An empty suggested_commands is better than commands with placeholder values.

Exception: When updating Lambda environment variables with `update_function_configuration`, if there are existing environment variables that you do NOT want to change, you MUST set their values to the exact string `"__PRESERVE__"`. The system will automatically ignore `"__PRESERVE__"` values and keep the current live values.
Example:
"Environment": {
  "Variables": {
    "DEFAULT_REGION": "ap-south-1",
    "SQS_QUEUE_URL": "__PRESERVE__",
    "DYNAMODB_TABLE_NAME": "__PRESERVE__"
  }
}

Respond ONLY with valid JSON, no markdown fences:
{
  "severity": "Critical | High | Medium | Low",
  "root_cause": "one sentence — what exactly failed",
  "what_it_means": "1-2 sentences — user/system impact",
  "how_to_fix": "numbered steps to fix this",
  "error_types": ["AccessDeniedException", ...],
  "is_infra_issue": true/false,
  "action": "action_name",
  "params": {},
  "confidence": 0.0,
  "failure_class": "resource_exhaustion | bad_deploy | dependency_failure | config_error | unknown",
  "suggested_commands": []
}

Confidence guide (0.0–1.0):
- 0.9+: unambiguous evidence in logs (OOM text, timeout text, throttle text)
- 0.7–0.9: strong signal but some uncertainty
- < 0.7: limited signal — use alert_only

Severity guide:
- Critical: data loss, security breach, complete outage
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

    # On Lambda: credentials come from the IAM role automatically.
    # Locally: set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or use `aws configure`.
    bedrock = boto3.client("bedrock-runtime", region_name=cfg.aws_region)

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
    # Fetch live Lambda metadata so the LLM has real values (role name, env vars, account)
    lambda_ctx = _get_lambda_context(function_name, region)

    lines = [
        f"## Lambda Function: `{function_name}`",
        f"## Log Group: `{log_group}`",
        f"## Region: `{region}`",
        f"## Error Count: {len(log_lines)}",
    ]
    if lambda_ctx:
        lines += [
            f"## IAM Role Name: `{lambda_ctx.get('role_name', 'unknown')}`",
            f"## IAM Role ARN: `{lambda_ctx.get('role_arn', 'unknown')}`",
            f"## Account ID: `{lambda_ctx.get('account_id', 'unknown')}`",
            f"## Lambda Timeout: {lambda_ctx.get('timeout', '?')}s",
            f"## Lambda Memory: {lambda_ctx.get('memory', '?')} MB",
        ]
        if lambda_ctx.get("env_var_names"):
            lines.append(f"## Env Var Keys: {', '.join(lambda_ctx['env_var_names'])}")

    # Rule-based pre-detection — inject high-confidence findings so the LLM can't miss them
    detected = _detect_config_issues(function_name, region, log_lines, lambda_ctx)
    if detected:
        lines.append("")
        lines.append("## DETECTED CONFIG ISSUES (high confidence, pre-analyzed):")
        for finding in detected:
            lines.append(f"- {finding}")
        lines.append("")
        lines.append(
            "IMPORTANT: The config issues above were detected automatically from live Lambda metadata."
            " Your suggested_commands MUST fix these specific issues using the exact values provided."
        )

    lines += [
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


def _detect_config_issues(
    function_name: str,
    region: str,
    log_lines: list[str],
    lambda_ctx: dict[str, Any] | None,
) -> list[str]:
    """
    Rule-based pre-detection of common Lambda configuration problems.
    Returns a list of human-readable finding strings to inject into the prompt.
    Each finding is specific and actionable — the LLM only needs to generate the boto3 call.

    Runs BEFORE the LLM so findings are deterministic, not guessed.
    """
    findings: list[str] = []
    if not lambda_ctx:
        return findings

    env_keys: set[str] = set(lambda_ctx.get("env_var_names", []))
    account_id = lambda_ctx.get("account_id", "unknown")
    role_name  = lambda_ctx.get("role_name", "unknown")

    # Join log lines for pattern matching
    log_text = "\n".join(log_lines).lower()

    # ── Rule 1: QueueDoesNotExist + DEFAULT_REGION missing ───────────────────
    # Most common: boto3 defaults to us-east-1 when DEFAULT_REGION is absent,
    # so the SQS client looks in the wrong region even though the queue exists.
    if (
        ("queuedoesnotexist" in log_text or "nonexistentqueue" in log_text)
        and "DEFAULT_REGION" not in env_keys
    ):
        findings.append(
            f"REGION MISMATCH: DEFAULT_REGION is NOT set in Lambda env vars. "
            f"boto3 is falling back to 'us-east-1' but all resources are in '{region}'. "
            f"Fix: add DEFAULT_REGION='{region}' via update_function_configuration. "
            f"Do NOT create a new queue — the queue likely exists in '{region}'."
        )

    # ── Rule 2: QueueDoesNotExist + SQS_QUEUE_URL missing ────────────────────
    # The env var that holds the queue URL was never set.
    if (
        ("queuedoesnotexist" in log_text or "nonexistentqueue" in log_text)
        and "SQS_QUEUE_URL" not in env_keys
        and "DEFAULT_REGION" in env_keys   # region is correct so it's a missing URL issue
    ):
        findings.append(
            f"MISSING ENV VAR: SQS_QUEUE_URL is not set in Lambda env vars. "
            f"The function has no queue URL to send messages to. "
            f"Fix: create the SQS queue (if missing) and set SQS_QUEUE_URL in update_function_configuration."
        )

    # ── Rule 3: AccessDeniedException — extract the exact action from logs ────
    import re
    access_denied = re.search(
        r"not authorized to perform: ([\w:]+) on resource: ([^\s]+)",
        "\n".join(log_lines),
        re.IGNORECASE,
    )
    if access_denied:
        iam_action   = access_denied.group(1)
        resource_arn = access_denied.group(2).rstrip(".")
        findings.append(
            f"MISSING IAM PERMISSION: '{iam_action}' on '{resource_arn}'. "
            f"Fix: add inline policy to role '{role_name}' "
            f"(account: {account_id}) via iam.put_role_policy."
        )

    # ── Rule 4: Timeout near the Lambda timeout setting ───────────────────────
    if "task timed out after" in log_text:
        configured_timeout = lambda_ctx.get("timeout", 0)
        findings.append(
            f"TIMEOUT: Lambda timed out. Current timeout is {configured_timeout}s. "
            f"Fix: increase timeout via update_function_configuration "
            f"(consider {min(configured_timeout * 3, 900)}s)."
        )

    # ── Rule 5: Memory near limit ─────────────────────────────────────────────
    oom_match = re.search(r"process exited before completing|runtime exited|oom", log_text)
    if oom_match:
        configured_memory = lambda_ctx.get("memory", 0)
        findings.append(
            f"OOM: Lambda ran out of memory. Current memory is {configured_memory}MB. "
            f"Fix: increase MemorySize to {min(configured_memory * 2, 10240)}MB "
            f"via update_function_configuration."
        )

    return findings


def _get_lambda_context(function_name: str, region: str) -> dict[str, Any] | None:
    """
    Fetch live Lambda configuration so the LLM has real values
    (role name, account ID, timeout, memory, env var keys) to use in suggested_commands.

    Returns a dict or None on any error (network issue, function not found, etc.).
    Never raises.
    """
    try:
        client = boto3.client("lambda", region_name=region)
        config = client.get_function_configuration(FunctionName=function_name)
        role_arn     = config.get("Role", "")
        # role ARN format: arn:aws:iam::<account-id>:role/<role-name>
        role_name    = role_arn.split("/")[-1] if "/" in role_arn else role_arn
        account_id   = role_arn.split(":")[4] if role_arn.count(":") >= 4 else "unknown"
        env_vars     = config.get("Environment", {}).get("Variables", {})
        return {
            "role_arn":     role_arn,
            "role_name":    role_name,
            "account_id":   account_id,
            "timeout":      config.get("Timeout", "?"),
            "memory":       config.get("MemorySize", "?"),
            "env_var_names": list(env_vars.keys()),
        }
    except Exception as exc:
        logger.debug(f"[LogAnalyst] Could not fetch Lambda context for {function_name}: {exc}")
        return None


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse(raw: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        logger.warning(f"[LogAnalyst] JSON parse failed: {exc} — raw: {raw[:200]}")
        return _SAFE_DEFAULT.copy()

    data.setdefault("severity",      "Unknown")
    data.setdefault("root_cause",    "Unknown")
    data.setdefault("what_it_means", "")
    data.setdefault("how_to_fix",    "")
    data.setdefault("error_types",   [])
    data.setdefault("is_infra_issue", False)
    # Action fields consumed by remediation_agent
    data.setdefault("action",        "alert_only")
    data.setdefault("params",        {})
    data.setdefault("failure_class", "unknown")
    data.setdefault("suggested_commands", [])
    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    # Ensure suggested_commands is always a list
    if not isinstance(data["suggested_commands"], list):
        data["suggested_commands"] = []
    return data
