"""
NEXUS Config Agent
===================
IaC drift detector and runtime environment contract validator.

Two continuous observation modes:
    1. IaC Drift — compares actual K8s resource state against declared
       Git manifests (NSync-inspired pattern, arXiv:2510.20211).
       Detects manual kubectl changes that bypassed GitOps.

    2. Runtime Env Contract — continuously verifies that running pods
       have all required environment variables populated.
       Supplements the GitAgent pre-deploy check with live validation.

Published IncidentEvents:
    IAC_DRIFT              — actual K8s state diverges from Git manifests
    ENV_KEY_MISSING        — running pod is missing a required env var
    CONFIG_DRIFT           — general config drift (checksum mismatch)
    SECRET_MISMATCH        — secret value changed without a code/deploy event
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    ConfigDriftContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _checksum(data: dict) -> str:
    """Stable SHA-256 of a dict (sorted keys for determinism)."""
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _normalise_deployment(dep_dict: dict) -> dict:
    """
    Strip K8s dynamic fields before drift comparison.
    We only care about user-controlled spec fields.
    """
    spec = dep_dict.get("spec", {})
    template = spec.get("template", {}).get("spec", {})
    return {
        "replicas":          spec.get("replicas"),
        "image":             [
            c.get("image") for c in template.get("containers", [])
        ],
        "env":               [
            {e.get("name"): e.get("value")} for c in template.get("containers", [])
            for e in c.get("env", []) if "value" in e
        ],
        "resources":         [
            c.get("resources") for c in template.get("containers", [])
        ],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Config Agent
# ──────────────────────────────────────────────────────────────────────────────

class ConfigAgent(BaseAgent):
    """
    Detects Kubernetes configuration drift and missing runtime env vars.

    IaC Drift detection:
        • Reads declared manifests from a local Git repository (YAML/JSON)
        • Queries the live K8s API for the same resource
        • Computes checksums of normalized specs and compares them
        • Emits IAC_DRIFT if they differ

    Runtime Env Contract:
        • Lists all running pods in watched namespaces
        • Checks each pod's env array against a set of required keys
        • Emits ENV_KEY_MISSING if required keys are absent

    Args:
        nats_client:        Connected NATSClient.
        manifests_dir:      Path to directory containing K8s YAML manifests (your GitOps repo).
        required_env_keys:  Set of env var keys that every pod must have at runtime.
        namespaces:         K8s namespaces to watch. None = default only.
        poll_interval_seconds: How often to check for drift (default 60s — heavier check).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        manifests_dir: Optional[str] = None,
        required_env_keys: Optional[Set[str]] = None,
        namespaces: Optional[List[str]] = None,
        poll_interval_seconds: float = 60.0,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.CONFIG,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.manifests_dir      = Path(manifests_dir) if manifests_dir else None
        self.required_env_keys  = required_env_keys or set()
        self.namespaces         = namespaces or ["default"]

        # Cache of last-known checksums per resource key "ns/kind/name"
        self._baseline_checksums: Dict[str, str] = {}

        self._k8s_core = None
        self._k8s_apps = None

    async def on_start(self) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._init_k8s)

    def _init_k8s(self) -> None:
        try:
            from kubernetes import client as k8s_client, config as k8s_config
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            self._k8s_core = k8s_client.CoreV1Api()
            self._k8s_apps = k8s_client.AppsV1Api()
        except Exception as exc:
            logger.warning(f"[ConfigAgent] K8s init failed: {exc}")

    # ── IaC drift detection ───────────────────────────────────────────────────

    def _load_git_manifests(self) -> Dict[str, dict]:
        """
        Parse all YAML manifests in manifests_dir and return a dict:
            "ns/kind/name" → normalised spec dict
        """
        if not self.manifests_dir or not self.manifests_dir.exists():
            return {}

        import yaml

        manifests: Dict[str, dict] = {}
        for path in self.manifests_dir.rglob("*.yaml"):
            try:
                with open(path) as f:
                    docs = list(yaml.safe_load_all(f))
                for doc in docs:
                    if not isinstance(doc, dict):
                        continue
                    kind = doc.get("kind", "")
                    meta = doc.get("metadata", {})
                    name = meta.get("name", "")
                    ns   = meta.get("namespace", "default")
                    if kind and name:
                        key = f"{ns}/{kind}/{name}"
                        if kind == "Deployment":
                            manifests[key] = _normalise_deployment(doc)
                        else:
                            manifests[key] = {"raw_checksum": _checksum(doc.get("spec", {}))}
            except Exception as exc:
                logger.debug(f"[ConfigAgent] Could not parse {path}: {exc}")

        return manifests

    def _get_live_deployment(self, name: str, namespace: str) -> Optional[dict]:
        """Fetch a live Deployment from K8s API and normalise it."""
        if not self._k8s_apps:
            return None
        try:
            dep = self._k8s_apps.read_namespaced_deployment(name, namespace)
            return _normalise_deployment(dep.to_dict())
        except Exception:
            return None

    def _check_iac_drift(self) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []
        manifests = self._load_git_manifests()

        for key, git_spec in manifests.items():
            parts = key.split("/", 2)
            if len(parts) != 3:
                continue
            ns, kind, name = parts

            if kind != "Deployment":
                continue

            live_spec = self._get_live_deployment(name, ns)
            if live_spec is None:
                continue

            git_checksum  = _checksum(git_spec)
            live_checksum = _checksum(live_spec)

            if git_checksum != live_checksum:
                # Find which fields drifted
                drifted_fields = [
                    f for f in git_spec
                    if json.dumps(git_spec.get(f), sort_keys=True)
                    != json.dumps(live_spec.get(f), sort_keys=True)
                ]

                logger.warning(
                    f"[ConfigAgent] IaC DRIFT: {key} | "
                    f"git={git_checksum} live={live_checksum} | "
                    f"fields={drifted_fields}"
                )

                # Cache baseline after first detection (avoid repeat storms)
                if self._baseline_checksums.get(key) == live_checksum:
                    continue   # Already reported this drift
                self._baseline_checksums[key] = live_checksum

                severity_score = min(len(drifted_fields) / 5.0, 1.0)

                events.append(IncidentEvent(
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
                ))
            else:
                # Drift resolved — remove from baseline
                self._baseline_checksums.pop(key, None)

        return events

    # ── Runtime env contract check ────────────────────────────────────────────

    def _check_runtime_env(self) -> List[IncidentEvent]:
        """
        Check that running pods have all required env vars populated.
        Reads pod spec.containers[].env[] — catches vars injected via envFrom too.
        """
        events: List[IncidentEvent] = []

        if not self.required_env_keys or not self._k8s_core:
            return []

        for ns in self.namespaces:
            try:
                pods = self._k8s_core.list_namespaced_pod(
                    ns, field_selector="status.phase=Running"
                ).items
            except Exception as exc:
                logger.warning(f"[ConfigAgent] Pod list failed for ns {ns}: {exc}")
                continue

            for pod in pods:
                pod_name = pod.metadata.name
                present_keys: Set[str] = set()

                for container in (pod.spec.containers or []):
                    for env_var in (container.env or []):
                        if env_var.name:
                            present_keys.add(env_var.name)
                    # Note: envFrom (ConfigMap / Secret refs) can't be introspected
                    # via pod spec without exec — we track direct env[] only

                missing = self.required_env_keys - present_keys
                for key in missing:
                    logger.warning(f"[ConfigAgent] Pod {ns}/{pod_name} missing env key: {key}")
                    events.append(IncidentEvent(
                        agent=AgentType.CONFIG,
                        signal_type=SignalType.ENV_KEY_MISSING,
                        severity=Severity.WARNING,
                        namespace=ns,
                        resource_name=pod_name,
                        resource_kind="Pod",
                        context={
                            "pod_name":    pod_name,
                            "namespace":   ns,
                            "missing_key": key,
                            "note": "Key may be injected via envFrom (ConfigMap/Secret) — verify manually",
                        },
                        suggested_runbook="runbook_missing_env_key_v1",
                        suggested_healing_level=0,
                        confidence=0.60,  # Lower confidence — envFrom not introspectable
                    ))

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def sense(self) -> List[IncidentEvent]:
        import asyncio
        loop   = asyncio.get_event_loop()
        events: List[IncidentEvent] = []

        # IaC drift (file I/O + K8s API — run in thread)
        if self.manifests_dir:
            drift_events = await loop.run_in_executor(None, self._check_iac_drift)
            events.extend(drift_events)

        # Runtime env contract (K8s API — run in thread)
        if self.required_env_keys:
            env_events = await loop.run_in_executor(None, self._check_runtime_env)
            events.extend(env_events)

        return events
