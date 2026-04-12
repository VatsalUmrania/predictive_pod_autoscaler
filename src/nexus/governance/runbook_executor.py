"""
NEXUS Runbook Executor — Phase 3 (Full Governance Plane)
=========================================================
Upgraded from the Phase 1 seed executor. Every action now travels through
the complete Governance Plane before and after execution:

    Event arrives (NATS)
        ↓
    RunbookLibrary.find_matching(event)
        ↓ (for each matching runbook)
    Pre-checks (Prometheus + event field assertions)
        ↓
    Per-action: ActionLadder.evaluate()
        ├── GovernanceCircuitBreaker (block if tripped)
        ├── PolicyEngine / OPA (allowlist, blast-radius, confidence)
        └── CooldownStore (prevent rapid re-execution)
        ↓
    RollbackRegistry.capture()     ← capture pre-state
        ↓
    AuditTrail.write_pending()     ← immutable record before action
        ↓
    Execute action (K8s API)
        ↓
    Post-checks (Prometheus SLO)
        ├── Pass → AuditTrail.update(success) + set cooldown
        │          ActionLadder.record_post_check_success()
        └── Fail → RollbackRegistry.rollback(pre_state)
                   AuditTrail.update(rolled_back)
                   ActionLadder.record_post_check_failure()
                   ← may trip GovernanceCircuitBreaker

Design principles:
    • Zero silent actions — audit trail written BEFORE execution
    • No autonomous L3 — requires confidence >= 0.85 or human approval
    • Governance CB after 3 consecutive post-check failures → escalate to human
    • Dry-run mode — logs all decisions without executing K8s API calls
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.action_ladder import ActionLadder
from nexus.governance.audit_trail import AuditTrail
from nexus.governance.policy_engine import PolicyEngine
from nexus.governance.rollback_registry import RollbackRegistry
from nexus.governance.runbook import Runbook, RunbookAction, RunbookLibrary

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Runbook Executor
# ──────────────────────────────────────────────────────────────────────────────

class RunbookExecutor:
    """
    Phase 3 governance-aware runbook executor.

    Args:
        nats_client:      Connected NATSClient.
        runbook_dir:      Path to directory containing runbook_*.yaml files.
        audit_trail:      Initialized AuditTrail.
        action_ladder:    Fully configured ActionLadder (policy + cooldown + CB + approval).
        rollback_registry: RollbackRegistry for pre-state capture + undo.
        prometheus_url:   Prometheus API base URL for pre/post checks.
        dry_run:          If True, skip K8s API calls (log decisions only).
        confidence:       Default confidence to pass to the policy engine (default 0.85).
                          Orchestrator overrides this in Phase 4.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        runbook_dir: Path,
        audit_trail: AuditTrail,
        action_ladder: ActionLadder,
        rollback_registry: RollbackRegistry,
        prometheus_url: str = "http://prometheus:9090",
        dry_run: bool = False,
        confidence: float = 0.85,
    ):
        self.nats             = nats_client
        self.audit            = audit_trail
        self.ladder           = action_ladder
        self.rollback_reg     = rollback_registry
        self.prom_url         = prometheus_url
        self.dry_run          = dry_run
        self.confidence       = confidence

        # RunbookLibrary — validates + indexes all YAML files
        self.library = RunbookLibrary(runbook_dir)
        logger.info(
            f"[RunbookExecutor] Ready — "
            f"{self.library.count()} runbooks, dry_run={dry_run}"
        )

        # Kubernetes API (initialised lazily on first use)
        self._k8s_apps: Optional[k8s_client.AppsV1Api] = None
        self._k8s_core: Optional[k8s_client.CoreV1Api] = None
        self._k8s_ready = False

    # ── K8s client init ───────────────────────────────────────────────────────

    def _ensure_k8s(self) -> None:
        if self._k8s_ready:
            return
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        self._k8s_apps  = k8s_client.AppsV1Api()
        self._k8s_core  = k8s_client.CoreV1Api()
        self._k8s_ready = True

    # ── Pre / Post check execution ────────────────────────────────────────────

    async def _prometheus_query(self, promql: str) -> Optional[float]:
        """Execute a PromQL instant query. Returns scalar or None on failure."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{self.prom_url}/api/v1/query",
                    params={"query": promql},
                )
                resp.raise_for_status()
                results = resp.json().get("data", {}).get("result", [])
                if results:
                    return float(results[0]["value"][1])
        except Exception as exc:
            logger.warning(f"[RunbookExecutor] Prometheus query failed: {exc}")
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
        fn = ops.get(operator, lambda a, b: False)
        try:
            return fn(type(threshold)(value), threshold)
        except (TypeError, ValueError):
            return fn(value, threshold)

    async def _run_pre_checks(self, runbook: Runbook, event: IncidentEvent) -> bool:
        for check in runbook.pre_checks:
            if check.type == "prometheus_query" and check.query:
                val = await self._prometheus_query(check.query)
                if not self._compare(val, check.operator, check.threshold):
                    logger.info(
                        f"[RunbookExecutor] Pre-check FAIL ({check.type}): "
                        f"{check.query} {check.operator} {check.threshold} → got {val}"
                    )
                    return False

            elif check.type == "event_field" and check.field:
                val = event.context.get(check.field)
                cmp_val = check.value if check.value is not None else check.threshold
                if not self._compare(val, check.operator, cmp_val):
                    logger.info(
                        f"[RunbookExecutor] Pre-check FAIL (event_field): "
                        f"context.{check.field}={val} {check.operator} {cmp_val}"
                    )
                    return False

        return True

    async def _run_post_checks(self, runbook: Runbook) -> bool:
        if not runbook.post_checks:
            return True

        for check in runbook.post_checks:
            query = check.effective_query
            if not query:
                continue

            timeout  = check.timeout_seconds
            deadline = time.monotonic() + timeout
            passed   = False

            while time.monotonic() < deadline:
                val = await self._prometheus_query(query)
                if self._compare(val, check.operator, check.threshold):
                    logger.info(f"[RunbookExecutor] Post-check PASS: {query} {check.operator} {check.threshold}")
                    passed = True
                    break
                await asyncio.sleep(10)

            if not passed:
                logger.warning(
                    f"[RunbookExecutor] Post-check TIMEOUT after {timeout}s: "
                    f"{query} {check.operator} {check.threshold}"
                )
                return False

        return True

    # ── Action execution ──────────────────────────────────────────────────────

    async def _execute_action(
        self, action: RunbookAction, event: IncidentEvent
    ) -> Dict[str, Any]:
        """Execute a single RunbookAction. Returns result dict."""
        action_type = action.type
        params      = action.params
        namespace   = params.get("namespace") or event.namespace or "default"
        name        = params.get("name") or event.resource_name or ""
        result: Dict[str, Any] = {"type": action_type, "status": "unknown"}

        if self.dry_run:
            logger.info(f"[RunbookExecutor] DRY RUN: {action_type} on {namespace}/{name}")
            result.update(status="dry_run")
            return result

        self._ensure_k8s()
        logger.info(f"[RunbookExecutor] Executing: {action_type} on {namespace}/{name}")

        try:
            if action_type == "restart_pod":
                pod_name = params.get("pod_name") or event.context.get("pod_name", name)
                self._k8s_core.delete_namespaced_pod(pod_name, namespace)
                result.update(status="executed", message=f"Deleted pod {namespace}/{pod_name}")

            elif action_type == "kubectl_rollout_undo":
                patch = {"spec": {"template": {"metadata": {"annotations": {
                    "nexus.io/rollback-triggered": datetime.now(timezone.utc).isoformat()
                }}}}}
                self._k8s_apps.patch_namespaced_deployment(name, namespace, patch)
                result.update(status="executed", message=f"Rollback annotation applied to {namespace}/{name}")

            elif action_type == "scale_deployment":
                target_replicas = params.get("replicas")
                if target_replicas is None:
                    result.update(status="failed", error="'replicas' param required")
                else:
                    patch = {"spec": {"replicas": int(target_replicas)}}
                    self._k8s_apps.patch_namespaced_deployment_scale(name, namespace, patch)
                    result.update(status="executed", message=f"Scaled {namespace}/{name} → {target_replicas}")

            elif action_type == "flush_coredns_cache":
                pods = self._k8s_core.list_namespaced_pod(
                    "kube-system", label_selector="k8s-app=kube-dns"
                )
                deleted = 0
                for pod in pods.items:
                    self._k8s_core.delete_namespaced_pod(pod.metadata.name, "kube-system")
                    deleted += 1
                result.update(status="executed", message=f"Flushed CoreDNS: deleted {deleted} pods")

            elif action_type == "patch_annotation":
                annotations = {**params.get("annotations", {})}
                annotations["nexus.io/last-touched"]   = datetime.now(timezone.utc).isoformat()
                annotations["nexus.io/healing-system"] = "nexus-v1"
                patch = {"metadata": {"annotations": annotations}}
                kind  = params.get("kind", "Deployment")
                if kind == "Deployment":
                    self._k8s_apps.patch_namespaced_deployment(name, namespace, patch)
                elif kind == "Service":
                    self._k8s_core.patch_namespaced_service(name, namespace, patch)
                result.update(status="executed", message=f"Annotated {kind} {namespace}/{name}")

            elif action_type == "emit_alert":
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
                    },
                )
                await self.nats.publish(alert_event)
                result.update(status="executed", message="Alert published to NATS")

            else:
                result.update(status="skipped", message=f"Unknown action type: {action_type}")

        except Exception as exc:
            result.update(status="failed", error=str(exc))
            logger.error(f"[RunbookExecutor] Action {action_type} FAILED: {exc}")

        logger.info(f"[RunbookExecutor] {action_type} → {result['status']}")
        return result

    # ── Runbook execution (full governance) ───────────────────────────────────

    async def _execute_runbook(self, runbook: Runbook, event: IncidentEvent) -> None:
        """
        Execute a single runbook against an event, traversing the full
        governance plane for every action.
        """
        target      = f"{event.namespace or 'default'}/{event.resource_name or 'unknown'}"
        runbook_id  = runbook.id

        # ── Pre-checks ────────────────────────────────────────────────────────
        pre_ok = await self._run_pre_checks(runbook, event)
        if not pre_ok:
            logger.info(f"[RunbookExecutor] Pre-checks FAILED for {runbook_id} — skipping")
            return

        # ── Per-action governance loop ─────────────────────────────────────────
        action_results: List[Dict] = []
        execution_failed = False

        for action in runbook.actions:
            # ── ActionLadder evaluation ──────────────────────────────────────
            decision = await self.ladder.evaluate(
                runbook    = runbook,
                action     = action,
                event      = event,
                target     = target,
                confidence = self.confidence,
            )

            if decision.requires_approval:
                logger.warning(
                    f"[RunbookExecutor] L3 action STAGED for approval "
                    f"(approval_id={decision.approval_id}) — "
                    f"skipping remaining actions in {runbook_id}"
                )
                action_results.append({
                    "type":        action.type,
                    "status":      "pending_approval",
                    "approval_id": decision.approval_id,
                })
                break

            if not decision.can_proceed:
                logger.info(
                    f"[RunbookExecutor] Action BLOCKED by governance: "
                    f"{action.type} — {decision.denial_reason}"
                )
                action_results.append({
                    "type":   action.type,
                    "status": "governance_blocked",
                    "reason": decision.denial_reason,
                })
                if action.abort_on_failure:
                    execution_failed = True
                    break
                continue

            # ── Pre-state capture ────────────────────────────────────────────
            self._ensure_k8s()
            pre_state = await self.rollback_reg.capture(
                action_type = action.type,
                namespace   = event.namespace or "default",
                name        = event.resource_name or "",
                k8s_apps    = self._k8s_apps,
                k8s_core    = self._k8s_core,
            )

            # ── Write pending audit record (crash-safe) ──────────────────────
            action_id = await self.audit.write_pending(
                triggered_by     = "runbook_executor_v3",
                runbook_id       = runbook_id,
                healing_level    = runbook.healing_level,
                target           = target,
                pre_check_results = {"passed": True},
                incident_id      = event.correlation_id or event.event_id,
                action_id        = None,   # generate new UUID
            )

            # ── Execute action ───────────────────────────────────────────────
            result = await self._execute_action(action, event)
            action_results.append(result)

            if result["status"] == "failed" and action.abort_on_failure:
                await self.audit.update_outcome(
                    action_id,
                    execution_outcome = "failed",
                    action_results    = action_results,
                )
                execution_failed = True
                break

            # Update audit (intermediate — will be overwritten at post-check)
            await self.audit.update_outcome(
                action_id,
                execution_outcome = "executed",
                action_results    = action_results,
            )

        if execution_failed:
            logger.error(f"[RunbookExecutor] Runbook {runbook_id} ABORTED: action failed")
            return

        # ── Post-checks ───────────────────────────────────────────────────────
        post_ok       = await self._run_post_checks(runbook)
        rollback_done = False

        if not post_ok:
            self.ladder.record_post_check_failure()
            logger.warning(
                f"[RunbookExecutor] Post-checks FAILED for {runbook_id} "
                f"(CB state: {self.ladder.governance_cb.state})"
            )

            if runbook.rollback_if_post_check_fails and runbook.rollback_actions:
                for rb_action in runbook.rollback_actions:
                    rb_result = await self._execute_action(rb_action, event)
                    action_results.append(rb_result)

                # Also use RollbackRegistry for the last executed action
                # (this captures infra state restoral, not just runbook rollback_actions)
                if len(runbook.actions) > 0:
                    last_action = runbook.actions[0]  # Primary action
                    pre_state_ns  = event.namespace or "default"
                    pre_state_nm  = event.resource_name or ""
                    pre_state_cap = await self.rollback_reg.capture(
                        last_action.type, pre_state_ns, pre_state_nm,
                        self._k8s_apps, self._k8s_core
                    )
                    rb_infra = await self.rollback_reg.rollback(
                        pre_state_cap, self._k8s_apps, self._k8s_core
                    )
                    action_results.append(rb_infra)

                rollback_done = True
        else:
            self.ladder.record_post_check_success()

        # ── Finalise audit record ─────────────────────────────────────────────
        outcome = "success" if post_ok else ("rolled_back" if rollback_done else "failed")
        await self.audit.update_outcome(
            action_id,
            execution_outcome  = outcome,
            post_check_results = {"slo_restored": post_ok},
            rollback_triggered = rollback_done,
            action_results     = action_results,
        )

        # ── Set cooldown (only on non-failure outcomes) ───────────────────────
        if outcome in ("success", "rolled_back"):
            await self.ladder.set_cooldown(runbook, target)

        logger.info(
            f"[RunbookExecutor] Runbook {runbook_id} COMPLETE — "
            f"outcome={outcome} target={target}"
        )

    # ── Main event handler ────────────────────────────────────────────────────

    async def handle_event(self, event: IncidentEvent) -> None:
        """
        Process an IncidentEvent: find matching runbooks and execute them.
        Called by the NATS subscriber callback.
        """
        matching = self.library.find_matching(event)
        if not matching:
            return

        logger.info(
            f"[RunbookExecutor] Event {event.signal_type} matched "
            f"{len(matching)} runbook(s)"
        )

        # Execute runbooks concurrently (each has its own audit trail entry)
        await asyncio.gather(
            *[self._execute_runbook(rb, event) for rb in matching],
            return_exceptions=True,
        )

    # ── Entrypoint ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to NATS incident events and begin processing."""
        logger.info("[RunbookExecutor] Starting — subscribing to all incident events")
        await self.nats.subscribe(
            handler       = self.handle_event,
            agent_filter  = ">",            # Subscribe to all agents
            durable_name  = "governance-runbook-executor",
        )
        logger.info("[RunbookExecutor] Listening ...")
        await asyncio.Event().wait()  # Block until stopped


# ──────────────────────────────────────────────────────────────────────────────
# Factory — build a fully-configured executor from env + defaults
# ──────────────────────────────────────────────────────────────────────────────

def build_executor(
    nats_client: NATSClient,
    runbook_dir: Path,
    audit_trail: AuditTrail,
    prometheus_url: str = "http://prometheus:9090",
    opa_url: str = "http://localhost:8181",
    redis_url: Optional[str] = None,
    dry_run: bool = False,
    confidence: float = 0.85,
) -> RunbookExecutor:
    """
    Build a fully-configured RunbookExecutor with the complete Governance Plane.

    This is the preferred entry point — it wires all components together
    with sensible defaults.
    """
    from nexus.governance.action_ladder import (
        ActionLadder,
        GovernanceCircuitBreaker,
        HumanApprovalQueue,
    )
    from nexus.governance.cooldown_store import CooldownStore
    from nexus.governance.rollback_registry import RollbackRegistry

    cooldown  = CooldownStore(redis_url=redis_url)
    approval  = HumanApprovalQueue()
    cb        = GovernanceCircuitBreaker(failure_threshold=3)
    policy    = PolicyEngine(opa_url=opa_url)
    ladder    = ActionLadder(policy, cooldown, approval, cb)
    rollback  = RollbackRegistry(dry_run=dry_run)

    return RunbookExecutor(
        nats_client       = nats_client,
        runbook_dir       = runbook_dir,
        audit_trail       = audit_trail,
        action_ladder     = ladder,
        rollback_registry = rollback,
        prometheus_url    = prometheus_url,
        dry_run           = dry_run,
        confidence        = confidence,
    )
