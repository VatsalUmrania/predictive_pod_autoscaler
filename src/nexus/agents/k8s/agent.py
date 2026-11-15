"""
NEXUS Universal Kubernetes Event Watcher Agent
==============================================
Monitors Kubernetes v1.Event streams for Warning events across all workload and
infrastructure kinds (Pod, Deployment, StatefulSet, DaemonSet, Job, Node,
PersistentVolumeClaim, Ingress, CRDs) and emits generic SymptomSignals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.agents.base_agent import BaseAgent
from nexus.bus.events.enums import AgentType, Severity, SignalKind, SignalSource
from nexus.bus.events.event import IncidentEvent, SymptomSignal
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

SYSTEM_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})


class K8sAgent(BaseAgent):
    """
    Provider-agnostic Kubernetes Warning event watcher.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        namespaces: list[str] | None = None,
        poll_interval_seconds: float = 30.0,
        **kwargs: Any,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.K8S,
            poll_interval_seconds=poll_interval_seconds,
        )
        env_ns = os.getenv("NEXUS_WATCH_NAMESPACES")
        self.namespaces: list[str] | None
        if namespaces is not None:
            self.namespaces = namespaces
        elif env_ns:
            self.namespaces = [n.strip() for n in env_ns.split(",") if n.strip()]
        else:
            self.namespaces = None

        self._k8s_core: k8s_client.CoreV1Api | None = None
        self._seen_event_uids: set[str] = set()
        self._max_seen_cache: int = 1000

    async def on_start(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_k8s)
        logger.info(
            f"[K8sAgent] Watching Warning events in namespaces: "
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

    def _event_to_symptom_signal(self, event: Any) -> SymptomSignal:
        """Map a Kubernetes v1.Event to an open SymptomSignal."""
        obj = getattr(event, "involved_object", None)
        kind = getattr(obj, "kind", "Resource") if obj else "Resource"
        name = getattr(obj, "name", "unknown") if obj else "unknown"
        ns = str(
            getattr(obj, "namespace", None)
            or getattr(event.metadata, "namespace", "default")
            or "default"
        )
        api_ver = getattr(obj, "api_version", "v1") if obj else "v1"

        reason = getattr(event, "reason", "StateTransition")
        message = getattr(event, "message", "")
        count = getattr(event, "count", 1) or 1
        urn = f"k8s://{ns}/{api_ver}/{kind}/{name}"

        return SymptomSignal(
            source=SignalSource.KUBERNETES_API,
            kind=SignalKind.STATE_TRANSITION,
            provider="kubernetes",
            resource_urn=urn,
            region_or_namespace=ns,
            symptom_summary=f"[{kind}/{name}] {reason}: {message} (count={count})",
            raw_payload={
                "event_uid": getattr(event.metadata, "uid", None),
                "reason": reason,
                "message": message,
                "involved_object": {
                    "kind": kind,
                    "name": name,
                    "namespace": ns,
                    "api_version": api_ver,
                },
                "count": count,
                "type": getattr(event, "type", "Warning"),
            },
            severity=Severity.HIGH,
        )

    async def sense_symptoms(self) -> list[SymptomSignal]:
        """Fetch warning events across all kinds and produce SymptomSignals."""
        core = self._k8s_core
        if not core:
            return []

        loop = asyncio.get_running_loop()
        symptoms: list[SymptomSignal] = []

        try:
            if self.namespaces:
                events_list = []
                for ns in self.namespaces:

                    def _fetch(n: str = ns) -> Any:
                        return core.list_namespaced_event(
                            namespace=n, field_selector="type=Warning"
                        )

                    res = await loop.run_in_executor(None, _fetch)
                    events_list.extend(res.items)
            else:

                def _fetch_all() -> Any:
                    return core.list_event_for_all_namespaces(
                        field_selector="type=Warning"
                    )

                res = await loop.run_in_executor(None, _fetch_all)
                events_list = res.items

            for ev in events_list:
                uid = (
                    getattr(ev.metadata, "uid", None)
                    or f"{ev.involved_object.name}-{ev.reason}"
                )
                if uid in self._seen_event_uids:
                    continue
                self._seen_event_uids.add(uid)
                if len(self._seen_event_uids) > self._max_seen_cache:
                    self._seen_event_uids.clear()

                ns = getattr(ev.metadata, "namespace", "")
                if not self.namespaces and ns in SYSTEM_NAMESPACES:
                    continue

                symptoms.append(self._event_to_symptom_signal(ev))
        except Exception as exc:
            logger.warning(f"[K8sAgent] Failed to poll Kubernetes events: {exc}")

        return symptoms

    async def sense(self) -> list[IncidentEvent]:
        """Sense warning events and return normalized IncidentEvents for bus publish."""
        symptoms = await self.sense_symptoms()
        events: list[IncidentEvent] = []
        for sym in symptoms:
            raw = sym.raw_payload
            inv = raw.get("involved_object", {})
            events.append(
                IncidentEvent(
                    agent=AgentType.K8S,
                    signal_type="state_transition",
                    severity=getattr(sym.severity, "value", sym.severity),
                    namespace=sym.region_or_namespace,
                    resource_name=inv.get("name"),
                    resource_kind=inv.get("kind"),
                    context={
                        "symptom_signal": sym.model_dump(),
                        "summary": sym.symptom_summary,
                    },
                    raw=raw,
                    confidence=0.9,
                )
            )
        return events
