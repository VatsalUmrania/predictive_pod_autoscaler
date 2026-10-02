"""
NEXUS Kubernetes Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.agents.base_agent import BaseAgent
from nexus.agents.k8s.constants import SYSTEM_NAMESPACES
from nexus.agents.k8s.deployment_checker import check_deployment, check_hpa
from nexus.agents.k8s.pod_checker import check_pod
from nexus.bus.incident_event import AgentType, IncidentEvent
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class K8sAgent(BaseAgent):
    """
    Kubernetes event watcher agent.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        namespaces: list[str] | None = None,
        crashloop_threshold: int = 3,
        pending_threshold_min: float = 2.0,
        degraded_threshold_min: float = 1.0,
        hpa_maxed_threshold_min: float = 5.0,
        poll_interval_seconds: float = 30.0,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.K8S,
            poll_interval_seconds=poll_interval_seconds,
        )
        env_ns = os.getenv("NEXUS_WATCH_NAMESPACES")
        self.namespaces: list[str] | None = None
        if namespaces is not None:
            self.namespaces = namespaces
        elif env_ns:
            self.namespaces = [n.strip() for n in env_ns.split(",") if n.strip()]
        else:
            self.namespaces = None

        self.crashloop_threshold = crashloop_threshold
        self.pending_threshold_s = pending_threshold_min * 60.0
        self.degraded_threshold_s = degraded_threshold_min * 60.0
        self.hpa_maxed_threshold_s = hpa_maxed_threshold_min * 60.0

        self._pending_since: dict[str, float] = {}
        self._hpa_maxed_since: dict[str, float] = {}
        self._degraded_since: dict[str, float] = {}
        self._emitted_this_cycle: set[str] = set()

        self._k8s_core: k8s_client.CoreV1Api | None = None
        self._k8s_apps: k8s_client.AppsV1Api | None = None
        self._k8s_autoscaling: k8s_client.AutoscalingV2Api | None = None

    async def on_start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_k8s)
        logger.info(
            f"[K8sAgent] Watching namespaces: "
            f"{'ALL' if not self.namespaces else self.namespaces}"
        )

    async def on_stop(self) -> None:
        pass

    def _init_k8s(self) -> None:
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        self._k8s_core = k8s_client.CoreV1Api()
        self._k8s_apps = k8s_client.AppsV1Api()
        self._k8s_autoscaling = k8s_client.AutoscalingV2Api()

    def _check_pod(self, pod: Any) -> list[IncidentEvent]:
        return check_pod(
            pod=pod,
            pending_since=self._pending_since,
            emitted_this_cycle=self._emitted_this_cycle,
            pending_threshold_s=self.pending_threshold_s,
            crashloop_threshold=self.crashloop_threshold,
        )

    def _check_deployment(self, dep: Any) -> list[IncidentEvent]:
        return check_deployment(
            dep=dep,
            degraded_since=self._degraded_since,
            emitted_this_cycle=self._emitted_this_cycle,
            degraded_threshold_s=self.degraded_threshold_s,
        )

    def _check_hpa(self, hpa: Any) -> list[IncidentEvent]:
        return check_hpa(
            hpa=hpa,
            hpa_maxed_since=self._hpa_maxed_since,
            hpa_maxed_threshold_s=self.hpa_maxed_threshold_s,
        )

    async def _fetch_resources(
        self,
        loop: asyncio.AbstractEventLoop,
        namespaced_fn: Callable[..., Any],
        cluster_fn: Callable[..., Any],
    ) -> list[Any]:
        if self.namespaces:
            lists = await asyncio.gather(
                *[loop.run_in_executor(None, namespaced_fn, ns) for ns in self.namespaces],
                return_exceptions=True,
            )
            items = []
            for res in lists:
                if not isinstance(res, BaseException):
                    items.extend(res.items)
                else:
                    logger.warning(f"[K8sAgent] Resource list error: {res}")
            return items
        res = await loop.run_in_executor(None, cluster_fn)
        return res.items

    async def sense(self) -> list[IncidentEvent]:
        if not self._k8s_core or not self._k8s_apps or not self._k8s_autoscaling:
            return []

        events: list[IncidentEvent] = []
        self._emitted_this_cycle.clear()
        loop = asyncio.get_running_loop()

        # 1. Pods
        pods = await self._fetch_resources(
            loop, self._k8s_core.list_namespaced_pod, self._k8s_core.list_pod_for_all_namespaces
        )
        for pod in pods:
            if not self.namespaces and pod.metadata.namespace in SYSTEM_NAMESPACES:
                continue
            events.extend(self._check_pod(pod))

        # 2. Deployments
        deps = await self._fetch_resources(
            loop, self._k8s_apps.list_namespaced_deployment, self._k8s_apps.list_deployment_for_all_namespaces
        )
        for dep in deps:
            if not self.namespaces and dep.metadata.namespace in SYSTEM_NAMESPACES:
                continue
            events.extend(self._check_deployment(dep))

        # 3. HPAs
        try:
            hpas = await self._fetch_resources(
                loop,
                self._k8s_autoscaling.list_namespaced_horizontal_pod_autoscaler,
                self._k8s_autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces,
            )
            for hpa in hpas:
                if not self.namespaces and hpa.metadata.namespace in SYSTEM_NAMESPACES:
                    continue
                events.extend(self._check_hpa(hpa))
        except Exception as exc:
            logger.debug(f"[K8sAgent] HPA check skipped: {exc}")

        return events
