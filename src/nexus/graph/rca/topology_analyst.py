"""
Topology Analyst Agent correlating deployments, git revisions, and platform dependencies.
"""

from __future__ import annotations

from typing import Any


class TopologyAnalystAgent:
    """Specialist agent correlating deployments, git revisions, and platform dependencies."""

    def analyze(self, raw_signals: list[dict[str, Any]], live_config: dict[str, Any]) -> dict[str, Any]:
        findings: list[str] = []
        signatures: list[str] = []
        has_recent_deploy = False

        # 1. Correlate raw signals
        for sig in raw_signals:
            stype = str(sig.get("signal_type", "")).lower()
            if stype in ("deploy_event", "rollout_started", "image_updated"):
                has_recent_deploy = True
                findings.append(f"Recent deployment detected: {sig.get('resource_name', 'unknown')}")
                signatures.append("recent_deploy")
            elif stype in ("pod_crashloop", "crashloopbackoff"):
                signatures.append("crashloop")
            elif stype in ("pod_oomkilled", "oomkilled"):
                signatures.append("oom_signal")
            elif "dns" in stype:
                signatures.append("dns_anomaly")
                findings.append("DNS resolution anomalies detected in network topology")
            elif "db" in stype or "database" in stype:
                signatures.append("database_anomaly")
                findings.append("Database tier reporting connection saturation")

        # 2. Correlate live resource describe state
        desc = str(live_config.get("describe", "")).lower()
        if desc:
            if (
                "active rollout=true" in desc
                or "replicasets: 2" in desc
                or "scalingreplicaset" in desc
                or "newreplicaset" in desc
            ):
                has_recent_deploy = True
                findings.append("Active Kubernetes deployment rollout detected across multiple ReplicaSets")
                signatures.append("recent_deploy")
                signatures.append("rollout_stuck")

            if (
                "command:" in desc
                or "exit 1" in desc
                or "crashing" in desc
                or "command override" in desc
            ):
                has_recent_deploy = True
                findings.append("Container spec command override or broken startup script detected in deployment template")
                signatures.append("recent_deploy")
                signatures.append("spec_mutation")

            if "crashloopbackoff" in desc or "reason: error" in desc or "exit code 1" in desc:
                signatures.append("crashloop")
                findings.append("Container termination with non-zero exit code observed in workload describe")

        return {
            "agent": "TopologyAnalyst",
            "findings": findings,
            "signatures": signatures,
            "has_recent_deploy": has_recent_deploy,
            "memory_limit": live_config.get("memory_limit"),
            "replicas": live_config.get("replicas"),
        }
