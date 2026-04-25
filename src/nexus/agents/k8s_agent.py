"""
NEXUS Kubernetes Agent
=======================
Watches Kubernetes pod and deployment events to detect failures early.

Observation sources:
    • Pod list (polling)  — detects CrashLoop, OOMKilled, Pending>2min
    • Deployment list     — detects available < desired replicas, stuck rollouts
    • HPA list            — detects HPA at maxReplicas for extended periods

Integration with existing PPA operator:
    The K8sAgent wraps the PPA LSTM predictor as a scaling sub-module.
    Pre-scaling decisions from DBAgent / predictive models are routed through
    K8sAgent → Governance Plane → scale_deployment action.

Published IncidentEvents:
    POD_CRASHLOOP        — pod restart_count >= threshold
    POD_OOMKILLED        — pod last terminated reason is OOMKilled
    POD_PENDING          — pod stuck in Pending state > 2 minutes
    DEPLOYMENT_DEGRADED  — available < desired replicas
    ROLLOUT_STUCK        — deployment progressing condition False
    HPA_MAXED            — HPA currentReplicas == maxReplicas for > 5 min
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Set

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    IncidentEvent,
    PodFailureContext,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class K8sAgent(BaseAgent):
    """
    Kubernetes event watcher agent.

    Args:
        nats_client:              Connected NATSClient.
        namespaces:               List of namespaces to watch. None = all namespaces.
        crashloop_threshold:      Min restart count before emitting POD_CRASHLOOP (default 3).
        pending_threshold_min:    Minutes a pod can stay Pending before alerting (default 2).
        hpa_maxed_threshold_min:  Minutes at maxReplicas before alerting (default 5).
        poll_interval_seconds:    How often to query K8s API (default 30s).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        namespaces: Optional[List[str]] = None,
        crashloop_threshold: int = 3,
        pending_threshold_min: float = 2.0,
        hpa_maxed_threshold_min: float = 5.0,
        poll_interval_seconds: float = 30.0,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.K8S,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.namespaces              = namespaces     # None = all
        self.crashloop_threshold     = crashloop_threshold
        self.pending_threshold_s     = pending_threshold_min * 60.0
        self.hpa_maxed_threshold_s   = hpa_maxed_threshold_min * 60.0

        # Track when each pod first entered Pending and when HPA first maxed
        self._pending_since: Dict[str, float]   = {}   # "ns/pod" → monotonic ts
        self._hpa_maxed_since: Dict[str, float] = {}   # "ns/hpa" → monotonic ts

        # Event de-duplication: track which pod keys we already emitted for this cycle
        self._emitted_this_cycle: Set[str] = set()

        self._k8s_core: Optional[k8s_client.CoreV1Api]  = None
        self._k8s_apps: Optional[k8s_client.AppsV1Api]  = None
        self._k8s_autoscaling: Optional[k8s_client.AutoscalingV2Api] = None

    async def on_start(self) -> None:
        """Initialize Kubernetes API clients."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._init_k8s)
        logger.info(
            f"[K8sAgent] Watching namespaces: "
            f"{'ALL' if not self.namespaces else self.namespaces}"
        )

    def _init_k8s(self) -> None:
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        self._k8s_core        = k8s_client.CoreV1Api()
        self._k8s_apps        = k8s_client.AppsV1Api()
        self._k8s_autoscaling = k8s_client.AutoscalingV2Api()

    # ── Pod inspection ────────────────────────────────────────────────────────

    def _check_pod(self, pod) -> List[IncidentEvent]:
        """Inspect a single pod object for failure conditions."""
        events: List[IncidentEvent] = []
        name = pod.metadata.name
        ns   = pod.metadata.namespace
        key  = f"{ns}/{name}"

        if key in self._emitted_this_cycle:
            return []

        phase = (pod.status.phase or "").lower()

        # ── Pending too long ──────────────────────────────────────────────────
        if phase == "pending":
            now = time.monotonic()
            if key not in self._pending_since:
                self._pending_since[key] = now
            elif (now - self._pending_since[key]) > self.pending_threshold_s:
                self._emitted_this_cycle.add(key)
                events.append(IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_PENDING,
                    severity=Severity.WARNING,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="Pod",
                    context={"pod_name": name, "pending_minutes": (now - self._pending_since[key]) / 60.0},
                ))
        else:
            self._pending_since.pop(key, None)

        # ── Container statuses ────────────────────────────────────────────────
        for cs in (pod.status.container_statuses or []):
            restart_count = cs.restart_count or 0

            # CrashLoopBackOff
            waiting_reason = ""
            if cs.state and cs.state.waiting:
                waiting_reason = cs.state.waiting.reason or ""

            if waiting_reason == "CrashLoopBackOff" and restart_count >= self.crashloop_threshold:
                self._emitted_this_cycle.add(key)
                # Try to get deployment name from owner references
                deployment_name = self._get_deployment_name(pod) or name
                events.append(IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_CRASHLOOP,
                    severity=Severity.CRITICAL if restart_count >= 5 else Severity.WARNING,
                    namespace=ns,
                    resource_name=deployment_name,
                    resource_kind="Deployment",
                    context=PodFailureContext(
                        pod_name=name,
                        deployment_name=deployment_name,
                        restart_count=restart_count,
                        reason="CrashLoopBackOff",
                    ).model_dump(),
                    suggested_runbook="runbook_pod_crashloop_v1",
                    suggested_healing_level=1,
                    confidence=0.95,
                ))

            # OOMKilled (in last terminated state)
            elif (cs.last_state
                  and cs.last_state.terminated
                  and cs.last_state.terminated.reason == "OOMKilled"):
                self._emitted_this_cycle.add(key)
                deployment_name = self._get_deployment_name(pod) or name
                mem_limit = None
                try:
                    lim = cs.resources.limits.get("memory") if cs.resources and cs.resources.limits else None
                    if lim:
                        mem_limit = self._parse_memory_mi(lim)
                except Exception:
                    pass

                events.append(IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.POD_OOMKILLED,
                    severity=Severity.WARNING,
                    namespace=ns,
                    resource_name=deployment_name,
                    resource_kind="Deployment",
                    context=PodFailureContext(
                        pod_name=name,
                        deployment_name=deployment_name,
                        restart_count=restart_count,
                        exit_code=137,
                        reason="OOMKilled",
                        memory_limit_mi=mem_limit,
                    ).model_dump(),
                    suggested_runbook="runbook_pod_crashloop_v1",
                    suggested_healing_level=1,
                    confidence=0.95,
                ))

        return events

    @staticmethod
    def _get_deployment_name(pod) -> Optional[str]:
        """Extract the owning ReplicaSet → Deployment name from pod owner refs."""
        for ref in (pod.metadata.owner_references or []):
            if ref.kind == "ReplicaSet":
                # ReplicaSet names are <deployment>-<hash>; strip the hash suffix
                parts = ref.name.rsplit("-", 1)
                return parts[0] if len(parts) == 2 else ref.name
        return None

    @staticmethod
    def _parse_memory_mi(mem_str: str) -> float:
        """Parse K8s memory string (e.g. '512Mi', '1Gi') to MiB."""
        multipliers = {"Ki": 1/1024, "Mi": 1, "Gi": 1024, "Ti": 1024*1024}
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                return float(mem_str[:-len(suffix)]) * mult
        return float(mem_str) / (1024 * 1024)   # Assume bytes

    # ── Deployment inspection ─────────────────────────────────────────────────

    def _check_deployment(self, dep) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []
        name   = dep.metadata.name
        ns     = dep.metadata.namespace
        spec   = dep.spec
        status = dep.status

        desired   = (spec.replicas               or 1)
        available = (status.available_replicas    or 0)
        ready     = (status.ready_replicas        or 0)

        # Degraded
        if available < desired and desired > 0:
            events.append(IncidentEvent(
                agent=AgentType.K8S,
                signal_type=SignalType.DEPLOYMENT_DEGRADED,
                severity=Severity.CRITICAL if available == 0 else Severity.WARNING,
                namespace=ns,
                resource_name=name,
                resource_kind="Deployment",
                context={
                    "deployment":        name,
                    "desired_replicas":  desired,
                    "available_replicas": available,
                    "ready_replicas":    ready,
                },
            ))

        # Stuck rollout (Progressing condition = False)
        for cond in (status.conditions or []):
            if (cond.type == "Progressing"
                    and cond.status == "False"
                    and cond.reason == "ProgressDeadlineExceeded"):
                events.append(IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.ROLLOUT_STUCK,
                    severity=Severity.CRITICAL,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="Deployment",
                    context={
                        "deployment":        name,
                        "condition_reason":  cond.reason,
                        "condition_message": cond.message,
                    },
                ))

        return events

    # ── HPA inspection ────────────────────────────────────────────────────────

    def _check_hpa(self, hpa) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []
        name   = hpa.metadata.name
        ns     = hpa.metadata.namespace
        key    = f"{ns}/{name}"
        status = hpa.status
        spec   = hpa.spec

        current = status.current_replicas or 0
        maximum = spec.max_replicas or 0

        if current >= maximum > 0:
            now = time.monotonic()
            if key not in self._hpa_maxed_since:
                self._hpa_maxed_since[key] = now
            elif (now - self._hpa_maxed_since[key]) > self.hpa_maxed_threshold_s:
                events.append(IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type=SignalType.HPA_MAXED,
                    severity=Severity.CRITICAL,
                    namespace=ns,
                    resource_name=name,
                    resource_kind="HorizontalPodAutoscaler",
                    context={
                        "hpa_name":         name,
                        "current_replicas": current,
                        "max_replicas":     maximum,
                        "maxed_minutes":    (now - self._hpa_maxed_since[key]) / 60.0,
                    },
                ))
        else:
            self._hpa_maxed_since.pop(key, None)

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def sense(self) -> List[IncidentEvent]:
        if not self._k8s_core:
            return []

        events: List[IncidentEvent] = []
        self._emitted_this_cycle.clear()

        loop = asyncio.get_event_loop()

        # ── Pods ──────────────────────────────────────────────────────────────
        if self.namespaces:
            pod_lists = await asyncio.gather(
                *[loop.run_in_executor(None, self._k8s_core.list_namespaced_pod, ns)
                  for ns in self.namespaces],
                return_exceptions=True,
            )
            pods = []
            for pl in pod_lists:
                if isinstance(pl, Exception):
                    logger.warning(f"[K8sAgent] Pod list error: {pl}")
                else:
                    pods.extend(pl.items)
        else:
            result = await loop.run_in_executor(None, self._k8s_core.list_pod_for_all_namespaces)
            pods = result.items

        for pod in pods:
            events.extend(self._check_pod(pod))

        # ── Deployments ───────────────────────────────────────────────────────
        if self.namespaces:
            dep_lists = await asyncio.gather(
                *[loop.run_in_executor(None, self._k8s_apps.list_namespaced_deployment, ns)
                  for ns in self.namespaces],
                return_exceptions=True,
            )
            deployments = []
            for dl in dep_lists:
                if not isinstance(dl, Exception):
                    deployments.extend(dl.items)
        else:
            result = await loop.run_in_executor(None, self._k8s_apps.list_deployment_for_all_namespaces)
            deployments = result.items

        for dep in deployments:
            events.extend(self._check_deployment(dep))

        # ── HPAs ─────────────────────────────────────────────────────────────
        try:
            if self.namespaces:
                hpa_lists = await asyncio.gather(
                    *[loop.run_in_executor(None, self._k8s_autoscaling.list_namespaced_horizontal_pod_autoscaler, ns)
                      for ns in self.namespaces],
                    return_exceptions=True,
                )
                hpas = []
                for hl in hpa_lists:
                    if not isinstance(hl, Exception):
                        hpas.extend(hl.items)
            else:
                result = await loop.run_in_executor(
                    None, self._k8s_autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces
                )
                hpas = result.items

            for hpa in hpas:
                events.extend(self._check_hpa(hpa))

        except Exception as exc:
            logger.debug(f"[K8sAgent] HPA check skipped: {exc}")

        return events
