"""
Unit tests for the multi-turn ReAct Investigator Loop (diagnose.py).
Verifies:
  1. Multi-turn reasoning <-> tool execution loop for Kubernetes.
  2. Multi-turn reasoning <-> tool execution loop for AWS with Boto3 self-correction.
  3. Structured RCAReport generation with evidence citations.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from nexus.bus.events.enums import Severity, SignalKind, SignalSource
from nexus.bus.events.event import SymptomSignal
from nexus.graph.nodes.diagnose import rca_reasoner_node, rca_tool_executor_node
from nexus.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_react_investigator_k8s_loop():
    registry = ToolRegistry()
    mock_llm = MagicMock()
    mock_llm_bound = AsyncMock()
    mock_llm.bind_tools.return_value = mock_llm_bound

    signal = SymptomSignal(
        source=SignalSource.KUBERNETES_API,
        kind=SignalKind.STATE_TRANSITION,
        provider="kubernetes",
        resource_urn="k8s://prod/apps/v1/Deployment/order-service",
        region_or_namespace="prod",
        symptom_summary="CrashLoopBackOff observed on payment pod",
        severity=Severity.HIGH,
    )
    initial_state = {"symptom_signal": signal.model_dump(), "rca_steps": 0}

    # Turn 1: LLM chooses to call k8s_get_logs
    call_logs = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "k8s_get_logs",
                "args": {"pod_name": "order-service-789", "namespace": "prod"},
                "id": "call-1",
            }
        ],
    )
    # Turn 2: LLM concludes with RCAReport
    call_report = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RCAReport",
                "args": {
                    "root_cause_summary": "Out of memory in container order-worker (exit code 137)",
                    "affected_resource_urn": "k8s://prod/apps/v1/Deployment/order-service",
                    "provider": "kubernetes",
                    "confidence": 0.95,
                    "hypotheses_evaluated": [
                        "Bad DB connection string (falsified)",
                        "OOMKilled due to heap saturation (confirmed)",
                    ],
                    "evidence_chain": [
                        {
                            "tool_name": "k8s_get_logs",
                            "tool_arguments": {
                                "pod_name": "order-service-789",
                                "namespace": "prod",
                            },
                            "key_finding": "fatal error: runtime: out of memory",
                        }
                    ],
                    "pre_incident_resource_state": {
                        "limits": {"memory": "256Mi"},
                        "replicas": 3,
                    },
                    "requires_remediation": True,
                },
                "id": "call-2",
            }
        ],
    )

    mock_llm_bound.ainvoke.side_effect = [call_logs, call_report]

    mock_core = MagicMock()
    mock_core.read_namespaced_pod_log.return_value = (
        "fatal error: runtime: out of memory"
    )

    with patch(
        "nexus.tools.k8s.universal.get_default_k8s_clients",
        return_value=(MagicMock(), mock_core),
    ):
        # Step 1: Reasoner -> tool call
        out1 = await rca_reasoner_node(initial_state, llm=mock_llm, registry=registry)
        assert out1["rca_complete"] is False
        assert out1["rca_steps"] == 1

        # Step 2: Executor runs k8s_get_logs
        state2 = {**initial_state, **out1}
        out_exec = await rca_tool_executor_node(state2, registry=registry)
        assert len(out_exec["investigation_trace"]) == 1
        assert "out of memory" in out_exec["investigation_trace"][0]["output"]

        # Step 3: Reasoner -> concludes RCAReport
        state3 = {**state2, **out_exec}
        out_final = await rca_reasoner_node(state3, llm=mock_llm, registry=registry)
        assert out_final["rca_complete"] is True
        report = out_final["rca_report"]
        assert "Out of memory" in report["root_cause_summary"]
        assert len(report["evidence_chain"]) == 1
        assert report["confidence"] >= 0.9


@pytest.mark.asyncio
async def test_react_investigator_aws_loop_with_self_correction():
    registry = ToolRegistry()
    mock_llm = MagicMock()
    mock_llm_bound = AsyncMock()
    mock_llm.bind_tools.return_value = mock_llm_bound

    signal = SymptomSignal(
        source=SignalSource.AWS_CLOUDWATCH,
        kind=SignalKind.METRIC_ANOMALY,
        provider="aws",
        resource_urn="arn:aws:rds:us-east-1:123456789012:db:orders-db",
        region_or_namespace="us-east-1",
        symptom_summary="Database connection count breached threshold",
    )
    initial_state = {"symptom_signal": signal.model_dump(), "rca_steps": 0}

    # Turn 1: Invalid kwargs causing ParamValidationError
    call_invalid = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "aws_inspect_resource",
                "args": {
                    "service": "rds",
                    "operation": "describe_db_instances",
                    "parameters": {"InvalidParam": "bad"},
                },
                "id": "call-aws-1",
            }
        ],
    )
    # Turn 2: Self-corrected call
    call_corrected = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "aws_inspect_resource",
                "args": {
                    "service": "rds",
                    "operation": "describe_db_instances",
                    "parameters": {"DBInstanceIdentifier": "orders-db"},
                },
                "id": "call-aws-2",
            }
        ],
    )
    # Turn 3: Conclude
    call_conclude = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "RCAReport",
                "args": {
                    "root_cause_summary": "RDS MaxConnections exceeded on orders-db",
                    "affected_resource_urn": "arn:aws:rds:us-east-1:123456789012:db:orders-db",
                    "provider": "aws",
                    "confidence": 0.92,
                    "hypotheses_evaluated": [
                        "Exhaustion from connection leak (confirmed)"
                    ],
                    "evidence_chain": [
                        {
                            "tool_name": "aws_inspect_resource",
                            "tool_arguments": {"DBInstanceIdentifier": "orders-db"},
                            "key_finding": "MaxAllocatedStorage 100, current connections 100/100",
                        }
                    ],
                    "pre_incident_resource_state": {"DBInstanceClass": "db.t3.medium"},
                    "requires_remediation": True,
                },
                "id": "call-aws-3",
            }
        ],
    )

    mock_llm_bound.ainvoke.side_effect = [call_invalid, call_corrected, call_conclude]

    # Turn 1: Reasoner produces invalid args
    out1 = await rca_reasoner_node(initial_state, llm=mock_llm, registry=registry)
    # Executor catches validation error and returns hint
    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        from botocore.exceptions import ParamValidationError

        mock_client.describe_db_instances.side_effect = [
            ParamValidationError(report="Unknown parameter InvalidParam"),
            {
                "DBInstances": [
                    {
                        "DBInstanceIdentifier": "orders-db",
                        "DBInstanceClass": "db.t3.medium",
                    }
                ]
            },
        ]

        state1 = {**initial_state, **out1}
        exec1 = await rca_tool_executor_node(state1, registry=registry)
        assert "self_correction_hint" in str(exec1["investigation_trace"][0]["output"])

        # Turn 2: Corrected invocation
        state2 = {**state1, **exec1}
        out2 = await rca_reasoner_node(state2, llm=mock_llm, registry=registry)
        exec2 = await rca_tool_executor_node({**state2, **out2}, registry=registry)
        assert "orders-db" in str(exec2["investigation_trace"][1]["output"])

        # Turn 3: Conclude
        state3 = {**state2, **out2, **exec2}
        out_final = await rca_reasoner_node(state3, llm=mock_llm, registry=registry)
        assert out_final["rca_complete"] is True
        assert out_final["rca_report"]["provider"] == "aws"
