"""
Tests for LangGraph Multi-Agent RCA System
==========================================
Verifies that LogAnalyst, MetricsAnalyst, TopologyAnalyst, and RCALeadSynthesizer
collaboratively diagnose production incidents without static runbooks.
"""

from __future__ import annotations

import pytest

from nexus.graph.nodes.diagnose import diagnose_node
from nexus.graph.rca import (
    LogAnalystAgent,
    MetricsAnalystAgent,
    RCALeadSynthesizer,
    RCAResult,
    TopologyAnalystAgent,
    VALID_FAILURE_CLASSES,
)


def test_log_analyst_agent_oom_detection():
    agent = LogAnalystAgent()
    logs = [
        "2026-09-09T10:00:00Z Initializing worker pool",
        "2026-09-09T10:01:00Z fatal memory error: out of memory",
        "2026-09-09T10:01:05Z exit code 137",
    ]
    report = agent.analyze(logs, [])
    assert report["agent"] == "LogAnalyst"
    assert "oom_event" in report["signatures"]
    assert report["has_critical_error"] is True
    assert report["confidence_delta"] > 0


def test_log_analyst_agent_connection_pool_detection():
    agent = LogAnalystAgent()
    logs = [
        "2026-09-09T10:00:00Z Connected to db-replica-1",
        "2026-09-09T10:01:15Z Error: connection pool exhausted, max 100 reached",
    ]
    report = agent.analyze(logs, [])
    assert "connection_exhaustion" in report["signatures"]
    assert report["has_critical_error"] is True


def test_metrics_analyst_agent_breach_detection():
    agent = MetricsAnalystAgent()
    metrics = {
        "error_rate": 0.12,
        "p99_latency_ms": 1450.0,
        "memory_usage_pct": 92.5,
        "restart_count": 4,
    }
    report = agent.analyze(metrics, [])
    assert report["agent"] == "MetricsAnalyst"
    assert "elevated_error_rate" in report["signatures"]
    assert "latency_spike" in report["signatures"]
    assert "memory_saturation" in report["signatures"]
    assert "pod_restarts" in report["signatures"]


def test_topology_analyst_deploy_correlation():
    agent = TopologyAnalystAgent()
    raw_signals = [
        {"signal_type": "deploy_event", "resource_name": "checkout-api"},
        {"signal_type": "pod_crashloop", "resource_name": "checkout-api"},
    ]
    report = agent.analyze(raw_signals, {})
    assert report["agent"] == "TopologyAnalyst"
    assert report["has_recent_deploy"] is True
    assert "recent_deploy" in report["signatures"]
    assert "crashloop" in report["signatures"]


@pytest.mark.asyncio
async def test_rca_lead_synthesizer_bad_deploy():
    synthesizer = RCALeadSynthesizer()
    telemetry = {
        "recent_logs": ["panic: runtime error: nil pointer dereference"],
        "metrics": {"error_rate": 0.15, "restart_count": 3},
        "live_config": {},
    }
    events = [
        {"signal_type": "deploy_event", "resource_name": "checkout-api"},
        {"signal_type": "pod_crashloop", "resource_name": "checkout-api"},
    ]
    target = {"name": "checkout-api", "namespace": "production"}

    result: RCAResult = await synthesizer.investigate(
        telemetry=telemetry,
        events=events,
        target=target,
        platform="kubernetes",
    )

    assert isinstance(result, RCAResult)
    assert result.failure_class == "bad_deploy"
    assert result.healing_level == 2
    assert result.confidence >= 0.85
    assert result.suggested_action == "k8s_rollback_deployment"
    assert result.action_params["deployment_name"] == "checkout-api"
    assert result.failure_class in VALID_FAILURE_CLASSES


@pytest.mark.asyncio
async def test_diagnose_node_end_to_end():
    state = {
        "incident_id": "inc-test-multiagent-01",
        "platform": "kubernetes",
        "target": {"name": "orders-v2", "namespace": "default"},
        "events": [
            {"signal_type": "pod_oomkilled", "resource_name": "orders-v2", "agent": "k8s", "severity": "critical"}
        ],
        "telemetry": {
            "recent_logs": ["Container killed by OOMKilled signal", "exit code 137"],
            "metrics": {"memory_usage_pct": 98.0, "restart_count": 2},
            "live_config": {},
        },
    }

    out = await diagnose_node(state)
    assert "diagnosis" in out
    diag = out["diagnosis"]
    assert diag["failure_class"] == "resource_exhaustion"
    assert diag["confidence"] >= 0.80
    assert diag["suggested_action"] == "k8s_restart_deployment"
    assert out["fsm_state"] == "planning"
    assert len(out["audit_log"]) == 1
