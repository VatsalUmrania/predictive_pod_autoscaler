from datetime import datetime, timezone

import pytest

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.rca_engine import RCAEngine


@pytest.mark.asyncio
async def test_k8s_incident_rca_classification():
    engine = RCAEngine(api_key=None)  # Uses deterministic rule-based fallback
    now = datetime.now(timezone.utc)
    cluster = IncidentCluster(cluster_id="c-k8s", created_at=now, last_event_at=now)
    event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_CRASHLOOP,
        severity=Severity.CRITICAL,
        namespace="default",
        resource_name="auth-service",
    )
    cluster.add_event(event)

    result = await engine.analyze(cluster)
    assert result.failure_class == "bad_deploy"
    assert result.healing_level == 2
    assert result.runbook_id == "runbook_high_error_rate_post_deploy_v1"
    assert result.confidence >= 0.8


@pytest.mark.asyncio
async def test_aws_lambda_oom_rca_classification():
    engine = RCAEngine(api_key=None)
    now = datetime.now(timezone.utc)
    cluster = IncidentCluster(cluster_id="c-aws-oom", created_at=now, last_event_at=now)
    event = IncidentEvent(
        agent=AgentType.METRICS,
        signal_type=SignalType.LAMBDA_OOM,
        severity=Severity.CRITICAL,
        namespace="aws:us-east-1",
        resource_name="image-resizer",
    )
    cluster.add_event(event)

    result = await engine.analyze(cluster)
    assert result.failure_class == "resource_exhaustion"
    assert result.healing_level == 2
    assert result.runbook_id == "runbook_lambda_oom_v1"
    assert result.confidence >= 0.8


@pytest.mark.asyncio
async def test_aws_sqs_dlq_rca_classification():
    engine = RCAEngine(api_key=None)
    now = datetime.now(timezone.utc)
    cluster = IncidentCluster(cluster_id="c-aws-sqs", created_at=now, last_event_at=now)
    event = IncidentEvent(
        agent=AgentType.METRICS,
        signal_type=SignalType.SQS_DLQ_DEPTH_HIGH,
        severity=Severity.WARNING,
        namespace="aws:us-east-1",
        resource_name="order-events-dlq",
    )
    cluster.add_event(event)

    result = await engine.analyze(cluster)
    assert result.failure_class == "dependency_failure"
    assert result.healing_level == 2
    assert result.runbook_id == "runbook_sqs_dlq_v1"


def test_parse_structured_dual_domain_llm_json():
    engine = RCAEngine(api_key=None)
    mock_gemini_json = """
    ```json
    {
      "root_cause": "Lambda memory limit exceeded due to large PDF processing payload",
      "failure_class": "resource_exhaustion",
      "healing_level": 2,
      "runbook_id": "runbook_lambda_oom_v1",
      "confidence": 0.92,
      "reasoning": "Log lines demonstrate Runtime.Exit error with SIGKILL due to OOM.",
      "actions_to_avoid": ["restart_deployment"],
      "domain": "aws",
      "suggested_action": "aws_update_lambda_memory",
      "action_params": {"function_name": "pdf-processor", "memory_mb": 512},
      "rollback_plan": {"action": "aws_update_lambda_memory", "params": {"memory_mb": 256}}
    }
    ```
    """
    res = engine._parse_response(mock_gemini_json)
    assert res is not None
    assert res.domain == "aws"
    assert res.suggested_action == "aws_update_lambda_memory"
    assert res.action_params == {"function_name": "pdf-processor", "memory_mb": 512}
    assert res.rollback_plan == {"action": "aws_update_lambda_memory", "params": {"memory_mb": 256}}
    assert res.confidence == 0.92
    assert res.to_dict()["domain"] == "aws"
