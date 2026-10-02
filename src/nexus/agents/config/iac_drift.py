"""
IaC Drift Detection against Git Manifests
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nexus.agents.config.helpers import compute_checksum, normalise_deployment
from nexus.bus.incident_event import (
    AgentType,
    ConfigDriftContext,
    IncidentEvent,
    Severity,
    SignalType,
)

logger = logging.getLogger(__name__)


def load_git_manifests(manifests_dir: Path | None) -> dict[str, dict]:
    """
    Parse all YAML manifests in manifests_dir and return a dict:
        "ns/kind/name" -> normalised spec dict
    """
    if not manifests_dir or not manifests_dir.exists():
        return {}

    import yaml

    manifests: dict[str, dict] = {}
    for path in manifests_dir.rglob("*.yaml"):
        try:
            with open(path) as f:
                docs = list(yaml.safe_load_all(f))
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind", "")
                meta = doc.get("metadata", {})
                name = meta.get("name", "")
                ns = meta.get("namespace", "default")
                if kind and name:
                    key = f"{ns}/{kind}/{name}"
                    if kind == "Deployment":
                        manifests[key] = normalise_deployment(doc)
                    else:
                        manifests[key] = {
                            "raw_checksum": compute_checksum(doc.get("spec", {}))
                        }
        except Exception as exc:
            logger.debug(f"[ConfigAgent] Could not parse {path}: {exc}")

    return manifests


def get_live_deployment(k8s_apps: Any, name: str, namespace: str) -> dict | None:
    """Fetch a live Deployment from K8s API and normalise it."""
    if not k8s_apps:
        return None
    try:
        dep = k8s_apps.read_namespaced_deployment(name, namespace)
        return normalise_deployment(dep.to_dict())
    except Exception:
        return None


def check_iac_drift(
    manifests_dir: Path | None,
    k8s_apps: Any,
    baseline_checksums: dict[str, str],
) -> list[IncidentEvent]:
    events: list[IncidentEvent] = []
    manifests = load_git_manifests(manifests_dir)

    for key, git_spec in manifests.items():
        parts = key.split("/", 2)
        if len(parts) != 3:
            continue
        ns, kind, name = parts

        if kind != "Deployment":
            continue

        live_spec = get_live_deployment(k8s_apps, name, ns)
        if live_spec is None:
            continue

        git_checksum = compute_checksum(git_spec)
        live_checksum = compute_checksum(live_spec)

        if git_checksum != live_checksum:
            drifted_fields = [
                f
                for f in git_spec
                if json.dumps(git_spec.get(f), sort_keys=True)
                != json.dumps(live_spec.get(f), sort_keys=True)
            ]

            logger.warning(
                f"[ConfigAgent] IaC DRIFT: {key} | "
                f"git={git_checksum} live={live_checksum} | "
                f"fields={drifted_fields}"
            )

            if baseline_checksums.get(key) == live_checksum:
                continue
            baseline_checksums[key] = live_checksum

            severity_score = min(len(drifted_fields) / 5.0, 1.0)

            events.append(
                IncidentEvent(
                    agent=AgentType.CONFIG,
                    signal_type=SignalType.IAC_DRIFT,
                    severity=Severity.WARNING,
                    namespace=ns,
                    resource_name=name,
                    resource_kind=kind,
                    context=ConfigDriftContext(
                        resource_kind=kind,
                        resource_name=name,
                        namespace=ns,
                        drift_fields=drifted_fields,
                        expected_hash=git_checksum,
                        actual_hash=live_checksum,
                        drift_severity_score=severity_score,
                    ).model_dump(),
                    confidence=0.87,
                )
            )
        else:
            baseline_checksums.pop(key, None)

    return events
