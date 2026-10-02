"""
NEXUS Config Agent Implementation
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from nexus.agents.base_agent import BaseAgent
from nexus.agents.config.iac_drift import check_iac_drift
from nexus.agents.config.runtime_env import check_runtime_env
from nexus.bus.incident_event import AgentType, IncidentEvent
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class ConfigAgent(BaseAgent):
    """
    Detects Kubernetes configuration drift and missing runtime env vars.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        manifests_dir: str | None = None,
        required_env_keys: set[str] | None = None,
        namespaces: list[str] | None = None,
        poll_interval_seconds: float = 60.0,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.CONFIG,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.manifests_dir = Path(manifests_dir) if manifests_dir else None
        self.required_env_keys = required_env_keys or set()
        self.namespaces = namespaces or ["default"]

        self._baseline_checksums: dict[str, str] = {}
        self._k8s_core = None
        self._k8s_apps = None

    async def on_start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_k8s)

    async def on_stop(self) -> None:
        pass

    def _init_k8s(self) -> None:
        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()
            self._k8s_core = k8s_client.CoreV1Api()
            self._k8s_apps = k8s_client.AppsV1Api()
        except Exception as exc:
            logger.warning(f"[ConfigAgent] K8s init failed: {exc}")

    def _check_iac_drift(self) -> list[IncidentEvent]:
        return check_iac_drift(
            manifests_dir=self.manifests_dir,
            k8s_apps=self._k8s_apps,
            baseline_checksums=self._baseline_checksums,
        )

    def _check_runtime_env(self) -> list[IncidentEvent]:
        return check_runtime_env(
            k8s_core=self._k8s_core,
            namespaces=self.namespaces,
            required_env_keys=self.required_env_keys,
        )

    async def sense(self) -> list[IncidentEvent]:
        loop = asyncio.get_running_loop()
        events: list[IncidentEvent] = []

        if self.manifests_dir:
            drift_events = await loop.run_in_executor(None, self._check_iac_drift)
            events.extend(drift_events)

        if self.required_env_keys:
            env_events = await loop.run_in_executor(None, self._check_runtime_env)
            events.extend(env_events)

        return events
