"""
Phase 5 — Diagnosis Agent
==========================
Claude Sonnet analyzes the incident context and returns structured JSON RCA.

This agent does ONE job:
  Context dict  →  Claude Sonnet  →  Structured RCA dict

The agent NEVER touches AWS. It only reads context and thinks.

Output schema:
{
  "root_cause":     "Lambda memory exhausted — OOM kill",
  "failure_class":  "resource_exhaustion",
  "severity":       "High",
  "confidence":     0.93,
  "action":         "increase_memory",
  "params":         {"memory_mb": 512},
  "recommendations": ["Increase memory to 512MB", "Enable X-Ray tracing"],
  "reasoning":      "P99 duration equals timeout, OOM found in logs..."
}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import boto3

from aws.config import AgentConfig

logger = logging.getLogger(__name__)

# ── Safe default when Claude fails or returns bad JSON ────────────────────────
SAFE_DEFAULT: dict[str, Any] = {
    "root_cause":      "Unable to determine — LLM call failed or returned invalid JSON",
    "failure_class":   "unknown",
    "severity":        "Medium",
    "confidence":      0.0,
    "action":          "alert_only",
    "params":          {},
    "recommendations": ["Investigate manually — AI diagnosis unavailable"],
    "reasoning":       "Fallback: defaulting to alert_only due to diagnosis failure.",
}

_SYSTEM_PROMPT = """\
You are a Senior AWS DevOps Engineer embedded in an AIOps system called NEXUS.
You receive complete incident telemetry and must diagnose the root cause.

## Your task
Analyze the incident context and return a JSON object with your diagnosis.

## Rules
1. Be specific and technical. No generic advice.
2. Use chain-of-thought in the "reasoning" field.
3. Confidence 0.9+ only when the evidence is unambiguous (OOM text in logs, timeout text in logs).
4. If the problem is in a DEPENDENCY (DynamoDB throttling, downstream HTTP 5xx), set failure_class to "dependency_failure".
5. If a recent deploy correlates with the error spike, set failure_class to "bad_deploy".
6. Severity: Critical (data loss risk), High (user-facing), Medium (degraded), Low (internal only).

## Available actions
| action | when to use | params |
|--------|------------|--------|
| increase_memory | OOM errors, or duration near timeout (memory also speeds up compute) | {"memory_mb": int} |
| increase_timeout | Duration > 80% of configured timeout, no OOM signals | {"timeout_seconds": int} |
| rollback_alias | Error spike within 30 min of a deploy, confidence >= 0.85 | {"alias_name": "live"} |
| set_concurrency | Throttle count high, errors are specifically throttle errors | {"reserved_concurrent_executions": int} |
| replay_dlq | DLQ depth elevated, consumer Lambda error rate is now low | {"dlq_url": "...", "source_queue_url": "..."} |
| alert_only | Dependency failure (DynamoDB/external), or insufficient signal | {} |

## Output format — respond ONLY with valid JSON, no markdown, no explanation outside JSON
{
  "root_cause": "string",
  "failure_class": "resource_exhaustion | bad_deploy | dependency_failure | config_error | unknown",
  "severity": "Critical | High | Medium | Low",
  "confidence": 0.0,
  "action": "action_name",
  "params": {},
  "recommendations": ["string", "string"],
  "reasoning": "2-3 sentence chain-of-thought"
}
"""


def diagnose(context: dict[str, Any], cfg: AgentConfig | None = None) -> dict[str, Any]:
    """
    Call Claude Sonnet with the incident context and return structured RCA.

    Args:
        context: Output from collector.context_builder.build_context()
        cfg:     AgentConfig (loaded from env if None)

    Returns:
        RCA dict matching the output schema above.
        Returns SAFE_DEFAULT on any failure.
    """
    cfg = cfg or AgentConfig.load()
    resource = context.get("service", "unknown")
    signal = context.get("signal_type", "unknown")

    logger.info(f"[DiagnosisAgent] Diagnosing {resource} ({signal}) via Bedrock {cfg.bedrock_model}")

    user_message = _build_prompt(context)

    try:
        client = boto3.client("bedrock-runtime", region_name=cfg.aws_region)
        response = client.converse(
            modelId=cfg.bedrock_model,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            inferenceConfig={"maxTokens": 1024},
        )
        raw = response["output"]["message"]["content"][0]["text"].strip()
        logger.debug("[DiagnosisAgent] Raw response: %s", raw[:400])
        rca = _parse(raw)

    except Exception as exc:
        # AnthropicBedrock raises botocore / httpx errors; catch all and fall back.
        logger.error(f"[DiagnosisAgent] Bedrock call failed: {exc}")
        rca = {**SAFE_DEFAULT, "reasoning": f"LLM call failed: {exc}"}

    logger.info(
        f"[DiagnosisAgent] action={rca['action']} "
        f"confidence={rca['confidence']:.0%} "
        f"severity={rca['severity']}"
    )
    return rca


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict[str, Any]) -> str:
    lines = [
        f"## Incident",
        f"- Service: {ctx.get('service', 'unknown')}",
        f"- Signal: {ctx.get('signal_type', 'unknown')}",
        f"- Alarm: {ctx.get('alarm', 'unknown')}",
        f"- Region: {ctx.get('region', 'unknown')}",
    ]

    if ctx.get("alarm_info"):
        ai = ctx["alarm_info"]
        lines.append(f"- Alarm reason: {ai.get('reason', '')[:200]}")
        lines.append(f"- Recurring alarm: {ai.get('recurring', False)}")

    if ctx.get("metrics"):
        lines += ["", "## Metrics"]
        for k, v in ctx["metrics"].items():
            lines.append(f"- {k}: {v}")

    if ctx.get("logs"):
        lines += ["", "## Recent Error Logs (last 10 lines)"]
        for line in ctx["logs"][-10:]:
            lines.append(f"  {line[:250]}")

    if ctx.get("recently_deployed"):
        lines += ["", "## ⚠️ Recent Deployment Detected"]
        dep = ctx.get("deployment", {})
        recent_versions = dep.get("recent_versions", [])
        if recent_versions:
            latest = recent_versions[-1]
            lines.append(f"- Latest version: v{latest.get('version')} deployed at {latest.get('last_modified')}")

    if ctx.get("cloudtrail"):
        lines += ["", "## Recent CloudTrail Events"]
        for ev in ctx["cloudtrail"][:5]:
            lines.append(f"- {ev.get('event')} by {ev.get('user')} at {ev.get('timestamp')}")

    if ctx.get("dependencies", {}).get("has_unhealthy_deps"):
        lines += ["", "## ⚠️ Unhealthy Dependencies"]
        for dep in ctx["dependencies"].get("unhealthy_deps", []):
            lines.append(f"- {dep} is unhealthy")

    if ctx.get("traces"):
        t = ctx["traces"]
        lines += ["", "## X-Ray Traces"]
        lines.append(f"- Total traces: {t.get('total')} | Faults: {t.get('faults')} | Avg duration: {t.get('avg_duration_ms')}ms")

    return "\n".join(lines)


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse(raw: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        logger.warning(f"[DiagnosisAgent] JSON parse failed: {exc}")
        return SAFE_DEFAULT.copy()

    required = {"root_cause", "failure_class", "severity", "confidence", "action", "params"}
    if not required.issubset(data.keys()):
        logger.warning(f"[DiagnosisAgent] Missing fields: {required - data.keys()}")
        return SAFE_DEFAULT.copy()

    data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    data.setdefault("recommendations", [])
    data.setdefault("reasoning", "")
    return data
