"""
NEXUS Phase 1 Seed Runbook Executor
=====================================
Deterministic healing engine — no LLM, pure rule-based runbook matching.

This component:
1. Subscribes to all NEXUS incident events on NATS
2. Matches events against loaded runbook trigger conditions (YAML)
3. Runs pre-checks (Prometheus queries + event field assertions)
4. Executes ordered action list through the Kubernetes API
5. Runs post-checks with timeout (SLO validation)
6. Triggers rollback actions if post-checks fail
7. Writes every action to the audit trail

This is intentionally simple — it is the BASELINE the Orchestrator (Phase 4)
must demonstrably improve upon.

Supported action types (Phase 1):
    restart_pod           kubectl delete pod <name> -n <ns>
    kubectl_rollout_undo  patch deployment annotation to trigger rollback
    scale_deployment      patch deployment spec.replicas
    flush_coredns_cache   delete all CoreDNS pods in kube-system
    patch_annotation      add nexus.io/* annotations to a resource
    emit_alert            publish a CRITICAL IncidentEvent to NATS
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import yaml
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.bus.incident_event import AgentType, HealingLevel, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.audit_trail import AuditTrail

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Severity ordering (for minimum-severity filtering)
# ──────────────────────────────────────────────────────────────────────────────
_SEVERITY_ORDER = ["info", "warning", "critical", "emergency"]


# ──────────────────────────────────────────────────────────────────────────────
# Runbook Executor
# ──────────────────────────────────────────────────────────────────────────────

class RunbookExecutor:
    """
    Phase 1 deterministic runbook executor.

    Stateful:
        - Loaded runbooks (from YAML directory)
        - Cooldown registry (in-memory; resets on restart)
        - Kubernetes API clients
    """

    def __init__(
        self,
        nats_client: NATSClient,
        runbook_dir: Path,
        audit_trail: AuditTrail,
        prometheus_url: str = "http://prometheus:9090",
        dry_run: bool = False,
    ):
        self.nats           = nats_client
        self.audit          = audit_trail
        self.prom_url       = prometheus_url
        self.dry_run        = dry_run
        self.runbooks: List[Dict] = self._load_runbooks(runbook_dir)

        # action_key -> last execution timestamp
        self._cooldowns: Dict[str, float] = {}

        # Kubernetes API clients
        try:
            k8s_config.load_incluster_config()
            logger.info("[RunbookExecutor] Loaded in-cluster K8s config")
        except Exception:
            k8s_config.load_kube_config()
            logger.info("[RunbookExecutor] Loaded local kubeconfig")

        self._k8s_apps = k8s_client.AppsV1Api()
        self._k8s_core = k8s_client.CoreV1Api()

    # ── Runbook loading ───────────────────────────────────────────────────────

    def _load_runbooks(self, runbook_dir: Path) -> List[Dict]:
        """Load and validate all runbook YAML files from the given directory."""
        runbooks = []
        for yaml_file in sorted(runbook_dir.glob("runbook_*.yaml")):
            try:
                with open(yaml_file) as f:
                    data = yaml.safe_load(f)
                rb = data.get("runbook")
                if not rb or "id" not in rb:
                    logger.warning(f"[RunbookExecutor] Skipping invalid runbook: {yaml_file}")
                    continue
                runbooks.append(rb)
                logger.info(
                    f"[RunbookExecutor] Loaded: {rb['id']} "
                    f"(level={rb.get('healing_level', 0)}, "
                    f"triggers={rb.get('trigger', {}).get('signal_types', [])})"
                )
            except Exception as exc:
                logger.error(f"[RunbookExecutor] Failed to load {yaml_file}: {exc}")
        logger.info(f"[RunbookExecutor] {len(runbooks)} runbooks loaded")
        return runbooks

    # ── Matching ──────────────────────────────────────────────────────────────

    def _find_matching_runbooks(self, event: IncidentEvent) -> List[Dict]:
        """Return all runbooks whose trigger conditions match this event."""
        matches = []
        for rb in self.runbooks:
            trigger = rb.get("trigger", {})
            signal_types = trigger.get("signal_types", [])
            min_severity = trigger.get("severity_minimum", "info")

            # Signal type match
            if event.signal_type not in signal_types:
                continue

            # Severity gate
            if _SEVERITY_ORDER.index(event.severity) < _SEVERITY_ORDER.index(min_severity):
                continue

            matches.append(rb)

        return matches

    # ── Cooldown ──────────────────────────────────────────────────────────────

    def _in_cooldown(self, runbook_id: str, resource_key: str, cooldown_seconds: int) -> bool:
        """Returns True if this runbook+resource combo is in cooldown."""
        key = f"{runbook_id}::{resource_key}"
        last = self._cooldowns.get(key, 0)
        remaining = cooldown_seconds - (time.monotonic() - last)
        if remaining > 0:
            logger.info(f"[RunbookExecutor] Cooldown active: {runbook_id} ({remaining:.0f}s remaining)")
            return True
        return False

    def _set_cooldown(self, runbook_id: str, resource_key: str) -> None:
        key = f"{runbook_id}::{resource_key}"
        self._cooldowns[key] = time.monotonic()

    # ── Pre/Post checks ───────────────────────────────────────────────────────

    async def _run_pre_checks(self, checks: List[Dict], event: IncidentEvent) -> bool:
        for check in checks:
            check_type = check.get("type")

            if check_type == "prometheus_query":
                result = await self._query_prometheus(check["query"])
                if not self._compare(result, check.get("operator", "gt"), check.get("threshold", 0)):
                    logger.info(
                        f"[RunbookExecutor] Pre-check FAIL: {check['query']} "
                        f"{check.get('operator')} {check.get('threshold')} (got {result})"
                    )
                    return False

            elif check_type == "event_field":
                field_val = event.context.get(check["field"])
                if not self._compare(field_val, check.get("operator", "eq"), check.get("value")):
                    logger.info(
                        f"[RunbookExecutor] Pre-check FAIL: "
                        f"event.context.{check['field']}={field_val} {check.get('operator')} {check.get('value')}"
                    )
                    return False

        return True

    async def _run_post_checks(self, checks: List[Dict]) -> bool:
        if not checks:
            return True

        for check in checks:
            timeout   = check.get("timeout_seconds", 120)
            deadline  = time.monotonic() + timeout
            query     = check.get("metric_query") or check.get("query")
            operator  = check.get("operator", "lt")
            threshold = check.get("threshold", 0)

            passed = False
            while time.monotonic() < deadline:
                result = await self._query_prometheus(query)
                if self._compare(result, operator, threshold):
                    logger.info(f"[RunbookExecutor] Post-check PASS: {query}")
                    passed = True
                    break
                await asyncio.sleep(10)

            if not passed:
                logger.warning(f"[RunbookExecutor] Post-check TIMEOUT: {query}")
                return False

        return True

    async def _query_prometheus(self, query: str) -> Optional[float]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.prom_url}/api/v1/query",
                    params={"query": query},
                )
                data = resp.json()
                results = data.get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])
        except Exception as exc:
            logger.warning(f"[RunbookExecutor] Prometheus query failed: {query!r}: {exc}")
        return None

    @staticmethod
    def _compare(value: Any, operator: str, threshold: Any) -> bool:
        if value is None:
            return False
        ops: Dict[str, Callable] = {
            "gt":  lambda a, b: a > b,
            "gte": lambda a, b: a >= b,
            "lt":  lambda a, b: a < b,
            "lte": lambda a, b: a <= b,
            "eq":  lambda a, b: a == b,
            "ne":  lambda a, b: a != b,
        }
        fn = ops.get(operator)
        if fn is None:
            logger.warning(f"[RunbookExecutor] Unknown operator: {operator}")
            return False
        try:
            return fn(type(threshold)(value), threshold)
        except (TypeError, ValueError):
            return fn(value, threshold)

    # ── Action execution ──────────────────────────────────────────────────────

    async def _execute_action(self, action: Dict, event: IncidentEvent) -> Dict[str, Any]:
        action_type = action["type"]
        params      = action.get("params", {})
        namespace   = params.get("namespace") or event.namespace or "default"
        name        = params.get("name") or event.resource_name or ""
        result: Dict[str, Any] = {"type": action_type, "status": "unknown"}

        if self.dry_run:
            logger.info(f"[RunbookExecutor] DRY RUN: would execute {action_type} on {namespace}/{name}")
            result["status"] = "dry_run"
            return result

        logger.info(f"[RunbookExecutor] Executing: {action_type} on {namespace}/{name}")

        # ── restart_pod ──────────────────────────────────────────────────────
        if action_type == "restart_pod":
            pod_name = params.get("pod_name") or event.context.get("pod_name", name)
            try:
                self._k8s_core.delete_namespaced_pod(pod_name, namespace)
                result.update(status="executed", message=f"Deleted pod {namespace}/{pod_name} (will restart via ReplicaSet)")
            except Exception as exc:
                result.update(status="failed", error=str(exc))

        # ── kubectl_rollout_undo ─────────────────────────────────────────────
        elif action_type == "kubectl_rollout_undo":
            try:
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "nexus.io/rollback-triggered": datetime.now(timezone.utc).isoformat()
                                }
                            }
                        }
                    }
                }
                self._k8s_apps.patch_namespaced_deployment(name, namespace, patch)
                result.update(status="executed", message=f"Rollback annotation applied to {namespace}/{name}")
            except Exception as exc:
                result.update(status="failed", error=str(exc))

        # ── scale_deployment ─────────────────────────────────────────────────
        elif action_type == "scale_deployment":
            target_replicas = params.get("replicas")
            if target_replicas is None:
                result.update(status="failed", error="'replicas' param required for scale_deployment")
            else:
                try:
                    patch = {"spec": {"replicas": target_replicas}}
                    self._k8s_apps.patch_namespaced_deployment_scale(name, namespace, patch)
                    result.update(status="executed", message=f"Scaled {namespace}/{name} → {target_replicas} replicas")
                except Exception as exc:
                    result.update(status="failed", error=str(exc))

        # ── flush_coredns_cache ──────────────────────────────────────────────
        elif action_type == "flush_coredns_cache":
            try:
                pods = self._k8s_core.list_namespaced_pod(
                    "kube-system", label_selector="k8s-app=kube-dns"
                )
                deleted = 0
                for pod in pods.items:
                    self._k8s_core.delete_namespaced_pod(pod.metadata.name, "kube-system")
                    deleted += 1
                result.update(status="executed", message=f"Flushed CoreDNS cache: deleted {deleted} pods")
            except Exception as exc:
                result.update(status="failed", error=str(exc))

        # ── patch_annotation ─────────────────────────────────────────────────
        elif action_type == "patch_annotation":
            annotations = params.get("annotations", {})
            annotations["nexus.io/last-touched"] = datetime.now(timezone.utc).isoformat()
            annotations["nexus.io/healing-system"] = "nexus-v1"
            try:
                patch = {"metadata": {"annotations": annotations}}
                resource_kind = params.get("kind", "Deployment")
                if resource_kind == "Deployment":
                    self._k8s_apps.patch_namespaced_deployment(name, namespace, patch)
                elif resource_kind == "Service":
                    self._k8s_core.patch_namespaced_service(name, namespace, patch)
                result.update(status="executed", message=f"Annotated {resource_kind} {namespace}/{name}")
            except Exception as exc:
                result.update(status="failed", error=str(exc))

        # ── emit_alert ───────────────────────────────────────────────────────
        elif action_type == "emit_alert":
            try:
                alert_event = IncidentEvent(
                    agent=AgentType.ORCHESTRATOR,
                    signal_type=SignalType.THRESHOLD_BREACH,
                    severity=Severity.CRITICAL,
                    namespace=namespace,
                    resource_name=name,
                    correlation_id=event.correlation_id or event.event_id,
                    context={
                        "alert_message":   params.get("message", "NEXUS healing alert"),
                        "source_event_id": event.event_id,
                        "source_agent":    event.agent,
                        "source_signal":   event.signal_type,
                    },
                )
                await self.nats.publish(alert_event)
                result.update(status="executed", message="Alert published to NATS")
            except Exception as exc:
                result.update(status="failed", error=str(exc))

        else:
            result.update(status="skipped", message=f"Unknown action type: {action_type}")

        logger.info(f"[RunbookExecutor] Action {action_type}: {result['status']}")
        return result

    # ── Main handler ──────────────────────────────────────────────────────────

    async def handle_event(self, event: IncidentEvent) -> None:
        """
        Main entry point: receives an IncidentEvent and executes all matching runbooks.
        Called by the NATS subscriber callback.
        """
        matching = self._find_matching_runbooks(event)
        if not matching:
            return

        resource_key = f"{event.namespace}/{event.resource_name}"
        logger.info(
            f"[RunbookExecutor] Event {event.signal_type} matched "
            f"{len(matching)} runbook(s) for {resource_key}"
        )

        for runbook in matching:
            rb_id     = runbook["id"]
            level     = runbook.get("healing_level", 0)
            cooldown  = runbook.get("cooldown_seconds", 300)

            # ── Cooldown gate ────────────────────────────────────────────────
            if self._in_cooldown(rb_id, resource_key, cooldown):
                continue

            # ── Pre-checks ───────────────────────────────────────────────────
            pre_checks = runbook.get("pre_checks", [])
            pre_ok = await self._run_pre_checks(pre_checks, event)
            if not pre_ok:
                logger.info(f"[RunbookExecutor] Pre-checks failed for {rb_id}, skipping")
                continue

            # ── Write pending audit record ───────────────────────────────────
            action_id = await self.audit.write_pending(
                triggered_by="seed_runbook_executor",
                runbook_id=rb_id,
                healing_level=level,
                target=resource_key,
                pre_check_results={"passed": True},
                incident_id=event.correlation_id or event.event_id,
            )

            # ── Execute actions ──────────────────────────────────────────────
            action_results: List[Dict] = []
            execution_failed = False
            for action in runbook.get("actions", []):
                result = await self._execute_action(action, event)
                action_results.append(result)
                if result["status"] == "failed" and action.get("abort_on_failure", True):
                    logger.error(
                        f"[RunbookExecutor] Action {action['type']} FAILED in {rb_id}: "
                        f"{result.get('error')}"
                    )
                    execution_failed = True
                    break

            if execution_failed:
                await self.audit.update_outcome(
                    action_id,
                    execution_outcome="failed",
                    action_results=action_results,
                )
                continue

            # ── Post-checks ──────────────────────────────────────────────────
            post_checks   = runbook.get("post_checks", [])
            post_ok       = await self._run_post_checks(post_checks)
            rollback_done = False

            if not post_ok and runbook.get("rollback_if_post_check_fails", True):
                logger.warning(f"[RunbookExecutor] Post-checks FAILED for {rb_id} — triggering rollback")
                for rb_action in runbook.get("rollback_actions", []):
                    rb_result = await self._execute_action(rb_action, event)
                    action_results.append(rb_result)
                rollback_done = True

            # ── Update audit + set cooldown ──────────────────────────────────
            outcome = "success" if post_ok else ("rolled_back" if rollback_done else "failed")
            await self.audit.update_outcome(
                action_id,
                execution_outcome=outcome,
                post_check_results={"slo_restored": post_ok},
                rollback_triggered=rollback_done,
                action_results=action_results,
            )
            self._set_cooldown(rb_id, resource_key)

            logger.info(
                f"[RunbookExecutor] Runbook {rb_id} COMPLETE: "
                f"outcome={outcome}, action_id={action_id}"
            )

    # ── Start ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to all NATS incident events and begin processing."""
        logger.info("[RunbookExecutor] Starting — subscribing to all incident events")
        await self.nats.subscribe(
            handler=self.handle_event,
            agent_filter=">",
            durable_name="seed-runbook-executor",
        )
        logger.info("[RunbookExecutor] Listening. Waiting for incident events ...")
        await asyncio.Event().wait()  # Block forever
