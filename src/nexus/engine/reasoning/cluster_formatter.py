"""
Formatting utilities for IncidentCluster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.engine.reasoning.incident_cluster import IncidentCluster

_KEY_ORDER = [
    "current_rps",
    "error_rate",
    "threshold",
    "anomaly_score",
    "latency_p95_ms",
    "restart_count",
    "missing_keys",
    "sha",
    "author",
    "reason",
    "utilization_pct",
    "active_connections",
    "predicted_rps",
    "confidence",
    "raw_features",
]


def format_llm_context(cluster: IncidentCluster) -> str:
    """Render a compact incident report suitable for inclusion in an LLM prompt."""
    lines = [
        f"INCIDENT CLUSTER: {cluster.cluster_id}",
        f"Detected: {cluster.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Open for: {cluster.age_seconds:.0f}s",
        f"Namespace: {cluster.namespace or 'unknown'}",
        f"Primary resource: {cluster.primary_resource or 'unknown'}",
        f"Highest severity: {cluster.highest_severity.upper()}",
        f"Total signals: {len(cluster.events)} from {len(cluster.agent_types)} agent(s)",
        "",
        "SIGNALS:",
    ]

    for i, event in enumerate(cluster.events, 1):
        ctx_kv = ""
        if isinstance(event.context, dict):
            relevant = {k: event.context[k] for k in _KEY_ORDER if k in event.context}
            if relevant:
                parts = []
                for k, v in relevant.items():
                    if isinstance(v, float):
                        parts.append(f"{k}={v:.3f}")
                    elif isinstance(v, list):
                        parts.append(f"{k}=[{','.join(str(x) for x in v[:3])}]")
                    else:
                        parts.append(f"{k}={v}")
                ctx_kv = "  |  " + "  ".join(parts)

        lines.append(
            f"  {i:2d}. [{str(event.agent).upper():<14s}] "
            f"{str(event.signal_type).upper():<30s} "
            f"sev={str(event.severity).lower()}"
            f"{ctx_kv}"
        )

        if isinstance(event.context, dict) and "raw_features" in event.context:
            rf = event.context["raw_features"]
            lines.append(
                f"         raw: rps/rep={rf.get('rps_per_replica', 'N/A'):>7}  "
                f"cpu={rf.get('cpu_utilization_pct', 'N/A'):>6}  "
                f"rps={rf.get('requests_per_second', 'N/A'):>7}"
            )

    if cluster.has_deploy_event:
        for e in cluster.events:
            if "deploy" in str(e.signal_type).lower() and isinstance(e.context, dict):
                sha = e.context.get("sha", "unknown")
                author = e.context.get("author", "unknown")
                lines.append(f"\nRECENT DEPLOY: sha={str(sha)[:12]}  by={author}")
                break

    return "\n".join(lines)


def format_cluster_summary(cluster: IncidentCluster) -> dict[str, Any]:
    """Format incident cluster as a summary dictionary."""
    return {
        "cluster_id": cluster.cluster_id,
        "created_at": cluster.created_at.isoformat(),
        "age_seconds": round(cluster.age_seconds, 1),
        "event_count": len(cluster.events),
        "agent_types": sorted(cluster.agent_types),
        "signal_types": sorted(cluster.signal_types),
        "namespace": cluster.namespace,
        "primary_resource": cluster.primary_resource,
        "highest_severity": cluster.highest_severity,
        "has_deploy_event": cluster.has_deploy_event,
        "signal_agreement": round(cluster.signal_agreement_score(), 3),
    }


def compute_signal_agreement(cluster: IncidentCluster) -> float:
    """Measures how strongly independent signals agree on a root cause (0.0–1.0)."""
    if not cluster.events:
        return 0.0
    if len(cluster.events) == 1:
        return 0.35  # Single signal — conservative

    agent_count = len(cluster.agent_types)
    event_count = len(cluster.events)

    diversity = min(agent_count / 5.0, 1.0)
    volume = min(event_count / 5.0, 1.0)

    anomaly_scores = [
        float(e.context.get("anomaly_score", 0.5))
        for e in cluster.events
        if isinstance(e.context, dict) and "anomaly_score" in e.context
    ]
    avg_anomaly = (sum(anomaly_scores) / len(anomaly_scores)) if anomaly_scores else 0.5

    return diversity * 0.40 + volume * 0.30 + avg_anomaly * 0.30
