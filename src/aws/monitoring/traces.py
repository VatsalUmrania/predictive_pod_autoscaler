"""
Phase 2 — Monitoring: AWS X-Ray Traces
=========================================
Queries X-Ray for slow segments, faults, and errors.
Provides latency breakdown by subsegment (DynamoDB, HTTP, Lambda).

Enable X-Ray on your Lambda with:
    aws lambda update-function-configuration \\
        --function-name my-fn \\
        --tracing-config Mode=Active
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TraceSegment:
    """A single X-Ray trace with fault/error info."""
    trace_id: str
    duration_ms: float
    has_fault: bool          # 5xx / Lambda crash
    has_error: bool          # 4xx
    has_throttle: bool
    response_time_ms: float
    subsegments: list[dict] = field(default_factory=list)  # e.g. DynamoDB, S3 calls


@dataclass
class TraceSummary:
    """Summary of X-Ray traces for a service over a time window."""
    service: str
    window_minutes: int
    total_traces: int
    fault_count: int
    error_count: int
    throttle_count: int
    avg_duration_ms: float
    traces: list[TraceSegment] = field(default_factory=list)


def get_service_traces(
    xray_client,
    service_name: str,
    window_minutes: int = 5,
    max_traces: int = 10,
) -> TraceSummary:
    """
    Fetch recent X-Ray traces for a service.
    Returns TraceSummary with fault/error breakdown.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_minutes)

    traces: list[TraceSegment] = []
    fault_count = error_count = throttle_count = 0
    total_duration = 0.0

    try:
        # Get trace summaries first (lightweight)
        filter_expression = f'service("{service_name}")'
        resp = xray_client.get_trace_summaries(
            StartTime=start,
            EndTime=end,
            FilterExpression=filter_expression,
        )
        summaries = resp.get("TraceSummaries", [])[:max_traces]

        for s in summaries:
            duration_ms = float(s.get("Duration", 0)) * 1000
            has_fault = s.get("HasFault", False)
            has_error = s.get("HasError", False)
            has_throttle = s.get("HasThrottle", False)

            if has_fault:
                fault_count += 1
            if has_error:
                error_count += 1
            if has_throttle:
                throttle_count += 1
            total_duration += duration_ms

            traces.append(TraceSegment(
                trace_id=s.get("Id", ""),
                duration_ms=duration_ms,
                has_fault=has_fault,
                has_error=has_error,
                has_throttle=has_throttle,
                response_time_ms=float(s.get("ResponseTime", 0)) * 1000,
            ))

    except Exception as exc:
        logger.debug(f"[Traces] get_service_traces({service_name}): {exc}")

    avg_duration = (total_duration / len(traces)) if traces else 0.0

    return TraceSummary(
        service=service_name,
        window_minutes=window_minutes,
        total_traces=len(traces),
        fault_count=fault_count,
        error_count=error_count,
        throttle_count=throttle_count,
        avg_duration_ms=avg_duration,
        traces=traces,
    )


def get_trace_details(xray_client, trace_id: str) -> dict[str, Any]:
    """Fetch full trace with all subsegments for a specific trace ID."""
    try:
        resp = xray_client.batch_get_traces(Ids=[trace_id])
        traces = resp.get("Traces", [])
        if not traces:
            return {}
        # Extract subsegment names (DynamoDB, SQS, HTTP, etc.)
        segments = traces[0].get("Segments", [])
        subsegments = []
        for seg in segments:
            doc = seg.get("Document", "{}")
            import json
            try:
                doc_dict = json.loads(doc)
                for sub in doc_dict.get("subsegments", []):
                    subsegments.append({
                        "name": sub.get("name", ""),
                        "duration_ms": float(sub.get("end_time", 0) - sub.get("start_time", 0)) * 1000,
                        "fault": sub.get("fault", False),
                        "error": sub.get("error", False),
                    })
            except Exception:
                pass
        return {"trace_id": trace_id, "subsegments": subsegments}
    except Exception as exc:
        logger.debug(f"[Traces] get_trace_details({trace_id}): {exc}")
        return {}
