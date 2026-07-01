"""
Phase 4 — Collector: Context Builder
======================================
The most important component of Phase 4.

Assembles ALL monitoring data into one structured context dict
before the LLM is called. The LLM receives everything it needs
in a single prompt — no partial information.

Output schema:
{
  "service":     "payment-api",
  "alarm":       "LambdaTimeout",
  "region":      "us-east-1",
  "metrics":     { error_rate, throttles, duration, ... },
  "logs":        [ "ERROR: ...", "CRITICAL: ..." ],
  "deployment":  { current_config, recent_versions, aliases },
  "cloudtrail":  [ { event_name, user, timestamp }, ... ],
  "traces":      { fault_count, avg_duration_ms, ... },
  "dependencies":{ unhealthy_deps, dependencies },
  "alarm_info":  { reason, history }
}
"""

from __future__ import annotations

import logging
from typing import Any

import boto3

from aws.config import AgentConfig
from aws.monitoring import metrics, logs, alarms, cloudtrail, traces
from aws.collector import deployment, dependencies

logger = logging.getLogger(__name__)


def build_context(
    resource_name: str,
    signal_type: str,
    region: str | None = None,
    alarm_name: str | None = None,
    alarm_reason: str | None = None,
    cfg: AgentConfig | None = None,
) -> dict[str, Any]:
    """
    Build the complete incident context dict.

    Called by the collect node in the LangGraph graph.
    All boto3 clients are created here for this single invocation.

    Args:
        resource_name:  Lambda function name, DynamoDB table, or SQS queue name
        signal_type:    e.g. "lambda_error_rate_high", "sqs_dlq_depth_high"
        region:         AWS region override
        alarm_name:     CloudWatch alarm name (if triggered by EventBridge)
        alarm_reason:   Alarm reason string from EventBridge event
        cfg:            AgentConfig (loaded from env if None)

    Returns:
        Fully assembled context dict ready for the LLM prompt.
    """
    cfg = cfg or AgentConfig.load()
    region = region or cfg.aws_region
    w = cfg.cw_window_minutes

    logger.info(f"[ContextBuilder] Building context for {resource_name} ({signal_type})")

    # Create all boto3 clients once
    cw       = boto3.client("cloudwatch", region_name=region)
    cw_logs  = boto3.client("logs",       region_name=region)
    lambda_  = boto3.client("lambda",     region_name=region)
    sqs      = boto3.client("sqs",        region_name=region)
    xray     = boto3.client("xray",       region_name=region)
    ct       = boto3.client("cloudtrail", region_name=region)

    ctx: dict[str, Any] = {
        "service":      resource_name,
        "alarm":        alarm_name or signal_type,
        "signal_type":  signal_type,
        "region":       region,
        "alarm_reason": alarm_reason or "",
    }

    # ── Alarm info ────────────────────────────────────────────────────────────
    if alarm_name:
        alarm_info = alarms.describe_alarm(cw, alarm_name)
        history = alarms.get_alarm_history(cw, alarm_name, hours=24)
        ctx["alarm_info"] = {
            "state":    alarm_info.state if alarm_info else "ALARM",
            "reason":   alarm_info.reason if alarm_info else alarm_reason,
            "history":  history[:5],
            "recurring": len(history) > 3,
        }

    # ── Lambda context ────────────────────────────────────────────────────────
    if _is_lambda(signal_type):
        fn_cfg = lambda_.get_function_configuration(FunctionName=resource_name)
        timeout_ms = fn_cfg.get("Timeout", 3) * 1000
        memory_mb  = fn_cfg.get("MemorySize", 128)

        m = metrics.collect_lambda_metrics(
            cw, resource_name, timeout_ms=timeout_ms, memory_mb=memory_mb, window_minutes=w
        )
        log_summary = logs.get_lambda_logs(cw_logs, resource_name, window_minutes=w)
        deploy_ctx  = deployment.collect_lambda_deployment(lambda_, resource_name)
        trail       = cloudtrail.get_lambda_trail(ct, resource_name, hours=2)
        trace_sum   = traces.get_service_traces(xray, resource_name, window_minutes=w)
        dep_map     = dependencies.discover_dependencies(xray, resource_name, window_minutes=w)

        ctx["metrics"] = {
            "error_rate":       round(m.error_rate, 4),
            "invocations":      int(m.invocation_count),
            "errors":           int(m.error_count),
            "throttles":        int(m.throttle_count),
            "duration_p50_ms":  round(m.duration_p50_ms, 1),
            "duration_p99_ms":  round(m.duration_p99_ms, 1),
            "duration_max_ms":  round(m.duration_max_ms, 1),
            "timeout_ms":       timeout_ms,
            "memory_mb":        memory_mb,
        }
        ctx["logs"]         = log_summary.error_lines[:30]
        ctx["deployment"]   = deployment.to_context_dict(deploy_ctx)
        ctx["cloudtrail"]   = [
            {
                "event":     e.event_name,
                "user":      e.user,
                "timestamp": str(e.event_time),
            }
            for e in trail.events[:10]
        ]
        ctx["recently_deployed"] = trail.was_recently_deployed
        ctx["traces"] = {
            "total":    trace_sum.total_traces,
            "faults":   trace_sum.fault_count,
            "errors":   trace_sum.error_count,
            "avg_duration_ms": round(trace_sum.avg_duration_ms, 1),
        }
        ctx["dependencies"] = dependencies.to_context_dict(dep_map)

    # ── SQS / DLQ context ─────────────────────────────────────────────────────
    if _is_sqs(signal_type):
        q_url = _get_queue_url(sqs, resource_name)
        if q_url:
            m_sqs = metrics.collect_sqs_metrics(cw, resource_name, window_minutes=w)
            ctx["metrics"] = {
                "dlq_depth":          m_sqs.visible_messages,
                "not_visible":        m_sqs.not_visible_messages,
                "oldest_msg_age_sec": round(m_sqs.oldest_message_age_seconds, 1),
            }
            ctx["dlq_url"] = q_url

    # ── DynamoDB context ──────────────────────────────────────────────────────
    if _is_dynamo(signal_type):
        m_ddb = metrics.collect_dynamo_metrics(cw, resource_name, window_minutes=w)
        ctx["metrics"] = {
            "read_throttles":  m_ddb.read_throttles,
            "write_throttles": m_ddb.write_throttles,
            "system_errors":   m_ddb.system_errors,
        }

    logger.info(f"[ContextBuilder] Context ready — keys: {list(ctx.keys())}")
    return ctx


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_lambda(signal: str) -> bool:
    return any(s in signal for s in ("lambda", "apigw", "api_gw"))

def _is_sqs(signal: str) -> bool:
    return "sqs" in signal or "dlq" in signal

def _is_dynamo(signal: str) -> bool:
    return "dynamo" in signal

def _get_queue_url(sqs, queue_name: str) -> str | None:
    try:
        return sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    except Exception:
        return queue_name if queue_name.startswith("http") else None
