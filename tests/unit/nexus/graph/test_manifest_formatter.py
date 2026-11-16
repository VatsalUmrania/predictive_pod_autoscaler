"""
Unit tests for manifest_formatter.py
"""

from __future__ import annotations

import yaml

from nexus.graph.manifest_formatter import extract_diagnostic_logs, plan_to_yaml_manifest


def test_plan_to_yaml_manifest_scale_action():
    plan = {
        "summary": "Scale deployment",
        "actions": [
            {
                "tool_name": "k8s_scale_resource",
                "parameters": {
                    "namespace": "monitoring",
                    "deployment_name": "prometheus-grafana",
                    "replicas": 3,
                },
            }
        ],
    }
    manifest_yaml = plan_to_yaml_manifest(plan, target="monitoring/prometheus-grafana")
    parsed = yaml.safe_load(manifest_yaml)
    assert parsed["apiVersion"] == "apps/v1"
    assert parsed["kind"] == "Deployment"
    assert parsed["metadata"]["name"] == "prometheus-grafana"
    assert parsed["metadata"]["namespace"] == "monitoring"
    assert parsed["spec"]["replicas"] == 3


def test_plan_to_yaml_manifest_patch_limits():
    plan = {
        "summary": "Patch memory limit",
        "actions": [
            {
                "tool_name": "k8s_patch_resource_limits",
                "parameters": {
                    "namespace": "default",
                    "deployment_name": "shop-demo",
                    "memory_limit": "512Mi",
                },
            }
        ],
    }
    manifest_yaml = plan_to_yaml_manifest(plan, target="default/shop-demo")
    parsed = yaml.safe_load(manifest_yaml)
    assert parsed["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "512Mi"


def test_extract_diagnostic_logs():
    state = {
        "investigation_trace": [
            {
                "tool_name": "k8s_get_logs",
                "output": "Starting server...\nConnecting to DB...\nError: connection refused at port 5432\nFatal: DB unreachable",
            }
        ],
        "rca_report": {
            "evidence_chain": [
                {"tool_name": "k8s_get_logs", "key_finding": "Fatal: DB unreachable"}
            ]
        },
    }
    logs = extract_diagnostic_logs(state)
    assert "Fatal: DB unreachable" in logs
