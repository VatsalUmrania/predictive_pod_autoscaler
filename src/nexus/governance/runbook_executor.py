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
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from nexus.agents import k8s_tools
from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.bus.nats_client import NATSClient
from nexus.governance.action_ladder import ActionLadder, PendingApproval
from nexus.governance.audit_trail import AuditTrail
from nexus.governance.policy_engine import LLM_TOOL_LEVEL as _LLM_TOOL_LEVEL
from nexus.governance.policy_engine import PolicyEngine
from nexus.governance.rollback_registry import RollbackRegistry
from nexus.governance.runbook import Runbook, RunbookAction, RunbookLibrary, RunbookTrigger

logger = logging.getLogger(__name__)

def _adapt_tool_result(res: str) -> dict[str, str]:
    """Normalise a k8s_tools string return into the executor result-dict shape.

    k8s_tools returns "Error …" on failure and a success message otherwise.
    """
    if isinstance(res, str) and res.startswith("Error"):
        return {"status": "failed", "message": res}
    return {"status": "executed", "message": res}

# Runbook Executor
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
        audit_trail: AuditTrail,
        action_ladder: ActionLadder,
        rollback_registry: RollbackRegistry,
        runbook_dir: Path | None = None,
        library: RunbookLibrary | None = None,
        prometheus_url: str = "http://prometheus:9090",
        dry_run: bool = False,
        confidence: float = 0.85,
    ):
        self.nats = nats_client
        self.audit = audit_trail
        self.ladder = action_ladder
        self.rollback_reg = rollback_registry
        self.prom_url = prometheus_url
        self.dry_run = dry_run
        self.confidence = confidence

        # RunbookLibrary — optional explicit instance; otherwise construct from runbook_dir
        if library is not None:
            self.library = library
        elif runbook_dir is not None:
            self.library = RunbookLibrary(runbook_dir)
        else:
            raise ValueError(
                "RunbookExecutor requires either `library` or `runbook_dir`"
            )
        logger.info(
            f"[RunbookExecutor] Ready — "
            f"{self.library.count()} runbooks, dry_run={dry_run}"
        )

        # Kubernetes API (initialised lazily on first use)
        self._k8s_apps: k8s_client.AppsV1Api | None = None
        self._k8s_core: k8s_client.CoreV1Api | None = None
        self._k8s_ready = False

    # K8s client init
    def _ensure_k8s(self) -> None:
        if self._k8s_ready:
            return
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()
        self._k8s_apps = k8s_client.AppsV1Api()
        self._k8s_core = k8s_client.CoreV1Api()
        self._k8s_ready = True

    # Pre / Post check execution
    async def _prometheus_query(self, promql: str) -> float | None:
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
        ops: dict[str, Callable] = {
            "gt": lambda a, b: a > b,
            "gte": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "lte": lambda a, b: a <= b,
            "eq": lambda a, b: a == b,
            "ne": lambda a, b: a != b,
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

            timeout = check.timeout_seconds
            deadline = time.monotonic() + timeout
            passed = False

            while time.monotonic() < deadline:
                val = await self._prometheus_query(query)
                if self._compare(val, check.operator, check.threshold):
                    logger.info(
                        f"[RunbookExecutor] Post-check PASS: {query} {check.operator} {check.threshold}"
                    )
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

    # Action execution
    async def _execute_action(
        self, action: RunbookAction, event: IncidentEvent
    ) -> dict[str, Any]:
        """Execute a single RunbookAction. Returns result dict."""
        action_type = action.type
        params = action.params
        namespace = params.get("namespace") or event.namespace or "default"
        name = params.get("name") or event.resource_name or ""
        result: dict[str, Any] = {"type": action_type, "status": "unknown"}

        if self.dry_run:
            logger.info(
                f"[RunbookExecutor] DRY RUN: {action_type} on {namespace}/{name}"
            )
            result.update(status="dry_run")
            return result

        self._ensure_k8s()
        logger.info(f"[RunbookExecutor] Executing: {action_type} on {namespace}/{name}")

        try:
            if action_type == "restart_pod":
                pod_name = params.get("pod_name") or event.context.get("pod_name")
                if not pod_name:
                    dep_name = params.get("name") or event.resource_name or ""
                    pods = self._k8s_core.list_namespaced_pod(
                        namespace, label_selector=f"app={dep_name}"
                    ).items
                    if not pods:
                        all_pods = self._k8s_core.list_namespaced_pod(namespace).items
                        pods = [
                            p for p in all_pods
                            if any(
                                ref.kind == "ReplicaSet" and dep_name in ref.name
                                for ref in (p.metadata.owner_references or [])
                            )
                        ]
                    if pods:
                        target = next(
                            (p for p in pods if (p.status.phase or "").lower() != "running"),
                            pods[0],
                        )
                        pod_name = target.metadata.name
                    else:
                        result.update(status="skipped", message=f"No pods found for {namespace}/{dep_name}")
                        return result
                self._k8s_core.delete_namespaced_pod(pod_name, namespace)
                result.update(
                    status="executed", message=f"Deleted pod {namespace}/{pod_name}"
                )

            elif action_type == "kubectl_rollout_undo":
                # Kubernetes Python client 30+ dropped the typed rollback subresource.
                # Roll back manually: find the previous ReplicaSet and patch the
                # deployment template to its pod template. Equivalent to
                # `kubectl rollout undo --to-revision=<previous>`.
                ns_api = self._k8s_apps
                try:
                    dep = ns_api.read_namespaced_deployment(name, namespace)
                    selector = dep.spec.selector.match_labels or {}
                    label_str = ",".join(f"{k}={v}" for k, v in selector.items())
                    rs_list = ns_api.list_namespaced_replica_set(
                        namespace,
                        label_selector=label_str,
                    ).items
                    from datetime import datetime, timezone
                    _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
                    sorted_rs = sorted(
                        rs_list,
                        key=lambda r: r.metadata.creation_timestamp or _epoch,
                        reverse=True,
                    )
                    if len(sorted_rs) < 2:
                        result.update(
                            status="skipped",
                            message=f"No previous ReplicaSet found for {namespace}/{name}",
                        )
                    else:
                        prev_template = sorted_rs[1].spec.template
                        patch = {"spec": {"template": prev_template.to_dict()}}
                        ns_api.patch_namespaced_deployment(name, namespace, patch)
                        result.update(
                            status="executed",
                            message=f"Rolled back {namespace}/{name} to revision on ReplicaSet {sorted_rs[1].metadata.name}",
                        )
                except Exception as exc:
                    result.update(status="failed", error=str(exc))

            elif action_type == "scale_deployment":
                target_replicas = params.get("replicas")
                if target_replicas is None:
                    result.update(status="failed", error="'replicas' param required")
                else:
                    patch = {"spec": {"replicas": int(target_replicas)}}
                    self._k8s_apps.patch_namespaced_deployment_scale(
                        name, namespace, patch
                    )
                    result.update(
                        status="executed",
                        message=f"Scaled {namespace}/{name} → {target_replicas}",
                    )

            elif action_type == "flush_coredns_cache":
                pods = self._k8s_core.list_namespaced_pod(
                    "kube-system", label_selector="k8s-app=kube-dns"
                )
                deleted = 0
                for pod in pods.items:
                    self._k8s_core.delete_namespaced_pod(
                        pod.metadata.name, "kube-system"
                    )
                    deleted += 1
                result.update(
                    status="executed",
                    message=f"Flushed CoreDNS: deleted {deleted} pods",
                )

            elif action_type == "patch_annotation":
                annotations = {**params.get("annotations", {})}
                annotations["nexus.io/last-touched"] = datetime.now(
                    timezone.utc
                ).isoformat()
                annotations["nexus.io/healing-system"] = "nexus-v1"
                patch = {"metadata": {"annotations": annotations}}
                kind = params.get("kind", "Deployment")
                if kind == "Deployment":
                    self._k8s_apps.patch_namespaced_deployment(name, namespace, patch)
                elif kind == "Service":
                    self._k8s_core.patch_namespaced_service(name, namespace, patch)
                result.update(
                    status="executed", message=f"Annotated {kind} {namespace}/{name}"
                )

            elif action_type == "emit_alert":
                alert_event = IncidentEvent(
                    agent=AgentType.ORCHESTRATOR,
                    signal_type=SignalType.THRESHOLD_BREACH,
                    severity=Severity.CRITICAL,
                    namespace=namespace,
                    resource_name=name,
                    correlation_id=event.correlation_id or event.event_id,
                    context={
                        "alert_message": params.get("message", "NEXUS healing alert"),
                        "source_event_id": event.event_id,
                    },
                )
                await self.nats.publish(alert_event)
                result.update(status="executed", message="Alert published to NATS")

            # LLM-proposed tools (dispatch to k8s_tools)
            # These reuse the governed _execute_action switch so LLM proposals
            # and runbook actions share ONE execution path.
            elif action_type == "restart_deployment":
                ns = params.get("namespace") or namespace
                dep = params.get("deployment_name") or params.get("name") or name
                result.update(**_adapt_tool_result(
                    k8s_tools.restart_deployment(namespace=ns, deployment_name=dep)
                ))

            elif action_type == "scale_resource":
                ns = params.get("namespace") or namespace
                kind = params.get("resource_kind", "deployment")
                rname = params.get("resource_name") or name
                replicas = params.get("replicas")
                if replicas is None:
                    result.update(status="failed", error="'replicas' param required")
                else:
                    result.update(**_adapt_tool_result(
                        k8s_tools.scale_resource(
                            namespace=ns, resource_kind=kind,
                            resource_name=rname, replicas=int(replicas),
                        )
                    ))

            elif action_type == "rollback_deployment":
                ns = params.get("namespace") or namespace
                dep = params.get("deployment_name") or params.get("name") or name
                result.update(**_adapt_tool_result(
                    k8s_tools.rollback_deployment(namespace=ns, deployment_name=dep)
                ))

            elif action_type == "patch_configmap":
                ns = params.get("namespace") or namespace
                cm = params.get("configmap_name") or name
                result.update(**_adapt_tool_result(
                    k8s_tools.patch_configmap(
                        namespace=ns, configmap_name=cm,
                        patch_data=params.get("patch_data", {}),
                    )
                ))

            elif action_type == "cordon_node":
                node = params.get("node_name") or name
                result.update(**_adapt_tool_result(k8s_tools.cordon_node(node_name=node)))

            elif action_type == "drain_node":
                node = params.get("node_name") or name
                result.update(**_adapt_tool_result(k8s_tools.drain_node(node_name=node)))

            else:
                result.update(
                    status="skipped", message=f"Unknown action type: {action_type}"
                )

        except Exception as exc:
            result.update(status="failed", error=str(exc))
            logger.error(f"[RunbookExecutor] Action {action_type} FAILED: {exc}")

        logger.info(f"[RunbookExecutor] {action_type} → {result['status']}")
        return result

    # Governed re-dispatch of a human-approved action
    async def execute_approved(self, pending: PendingApproval) -> dict[str, Any]:
        """Run a single human-approved action through the full governance plane.

        The ONE place an approved action actually touches the cluster. Both
        rule-based runbooks (runbook looked up in the library) and LLM proposals
        (runbook_id == 'llm_dynamic', action synthesized from stored tool_args)
        flow through here. Governance still applies: cooldown, circuit breaker,
        and allowlist may block an action even after human approval — a human may
        approve without realising a heal ran 60s ago or that the breaker is open.
        human_approved flips the L3 confidence gate; override_blast_radius
        records that the human's sign-off IS the cluster-wide override.

        Returns a dict with ``status`` ∈ {success, failed, governance_blocked}
        plus ``action_id`` (the audit row) and ``result`` (the per-action dict).
        """
        # manual_review = the LLM staged a diagnosis for human eyes, not a
        # concrete remediation (orchestrator.py / llm_orchestrator.py both
        # enqueue it with healing_level=0). There is nothing to execute on the
        # cluster — the human's approval IS the outcome — so record an audit row
        # and resolve without touching k8s, instead of crashing in
        # _materialize_llm_pending on the unmapped "manual_review" tool.
        if pending.runbook_id == "llm_dynamic" and pending.action_type == "manual_review":
            action_id = await self.audit.write_pending(
                triggered_by="human:api_user",
                runbook_id="llm_dynamic",
                healing_level=0,
                target=pending.target,
                pre_check_results={
                    "passed": True,
                    "human_approved": True,
                    "manual_review": True,
                },
                incident_id=pending.incident_id,
                action_id=None,
            )
            await self.audit.update_outcome(
                action_id,
                execution_outcome="success",
                post_check_results={"manual_review": True, "note": "no cluster action"},
                action_results=[{"action": "manual_review", "status": "reviewed"}],
            )
            logger.info(
                f"[RunbookExecutor] manual_review approved {pending.approval_id} "
                f"for {pending.target} — no cluster action (human reviewed diagnosis)"
            )
            return {
                "status": "reviewed",
                "action_id": action_id,
                "result": {"action": "manual_review", "status": "no_cluster_action"},
            }

        runbook, action, event = self._materialize_pending(pending)
        target = f"{event.namespace or 'default'}/{event.resource_name or action.type}"
        confidence = pending.confidence

        decision = await self.ladder.evaluate(
            runbook=runbook,
            action=action,
            event=event,
            target=target,
            confidence=confidence,
            human_approved=True,
            override_blast_radius=True,
        )
        if not decision.can_proceed:
            logger.warning(
                f"[RunbookExecutor] Approved action BLOCKED post-approval: "
                f"{action.type} on {target} — {decision.denial_reason}"
            )
            return {
                "status": "governance_blocked",
                "reason": decision.denial_reason,
                "action_id": None,
            }

        self._ensure_k8s()
        await self.rollback_reg.capture(
            action_type=action.type,
            namespace=event.namespace or "default",
            name=event.resource_name or "",
            k8s_apps=self._k8s_apps,
            k8s_core=self._k8s_core,
        )
        action_id = await self.audit.write_pending(
            triggered_by="human:api_user",
            runbook_id=runbook.id,
            healing_level=runbook.healing_level,
            target=target,
            pre_check_results={"passed": True, "human_approved": True},
            incident_id=pending.incident_id,
            action_id=None,
        )
        result = await self._execute_action(action, event)

        # Post-checks: LLM-synthesized runbooks have none (→ pass); rule-based
        # runbooks re-run their SLO assertions, same as the autonomous path.
        post_ok = (
            await self._run_post_checks(runbook)
            if result["status"] != "failed"
            else False
        )
        if post_ok:
            self.ladder.record_post_check_success()
            outcome = "success"
            await self.ladder.set_cooldown(runbook, target)
        else:
            self.ladder.record_post_check_failure()
            outcome = "failed"
        await self.audit.update_outcome(
            action_id,
            execution_outcome=outcome,
            post_check_results={"slo_restored": post_ok},
            action_results=[result],
        )
        logger.info(
            f"[RunbookExecutor] Approved action {action.type} on {target} "
            f"→ outcome={outcome}"
        )
        return {"status": outcome, "action_id": action_id, "result": result}

    def _materialize_pending(
        self, pending: PendingApproval
    ) -> tuple[Runbook, RunbookAction, IncidentEvent]:
        """Reconstruct (runbook, action, event) from a staged PendingApproval.

        Rule-based approvals carry a full snapshot of the staged runbook action
        and triggering event in their context (see ActionLadder.evaluate). LLM
        approvals are synthesized from the stored tool name + args.
        """
        if pending.runbook_id == "llm_dynamic":
            return self._materialize_llm_pending(pending)

        ctx = pending.context or {}
        # Snapshot check first — a missing snapshot means we can't re-dispatch
        # regardless of whether the runbook is loaded. Surface that distinctly
        # from "runbook not in library".
        if "event" not in ctx or "action" not in ctx:
            raise ValueError(
                f"approval {pending.approval_id} (runbook={pending.runbook_id}) "
                "missing staged event/action snapshot — cannot re-dispatch"
            )
        runbook = self.library.get(pending.runbook_id)
        if runbook is None:
            raise ValueError(
                f"runbook {pending.runbook_id!r} not found in library — cannot re-dispatch"
            )
        action = RunbookAction.model_validate(ctx["action"])
        event = IncidentEvent.model_validate(ctx["event"])
        return runbook, action, event

    def _materialize_llm_pending(
        self, pending: PendingApproval
    ) -> tuple[Runbook, RunbookAction, IncidentEvent]:
        """Build a synthetic runbook+action+event for an LLM-proposed tool."""
        ctx = pending.context or {}
        tool_name = ctx.get("proposed_tool") or pending.action_type
        tool_args = ctx.get("tool_args", {}) or {}
        spec = _LLM_TOOL_LEVEL.get(tool_name)
        if spec is None:
            raise ValueError(
                f"unknown LLM tool {tool_name!r} — no governance level mapping"
            )
        level, blast_radius = spec
        action = RunbookAction(type=tool_name, params={**tool_args}, abort_on_failure=True)
        runbook = Runbook(
            id="llm_dynamic",
            description=f"LLM-proposed remediation: {tool_name}",
            healing_level=level,
            trigger=RunbookTrigger(),
            cooldown_seconds=300,
            blast_radius=blast_radius,
        )
        summary = ctx.get("cluster_summary", {}) or {}
        event = IncidentEvent(
            agent=AgentType.ORCHESTRATOR,
            signal_type=SignalType.THRESHOLD_BREACH,
            severity=Severity.WARNING,
            namespace=tool_args.get("namespace") or summary.get("namespace") or "default",
            resource_name=(
                tool_args.get("deployment_name")
                or tool_args.get("resource_name")
                or tool_args.get("configmap_name")
                or tool_args.get("node_name")
                or summary.get("primary_resource")
            ),
            correlation_id=summary.get("cluster_id") or pending.incident_id,
        )
        return runbook, action, event

    # Runbook execution (full governance)
    async def _execute_runbook(self, runbook: Runbook, event: IncidentEvent) -> None:
        """
        Execute a single runbook against an event, traversing the full
        governance plane for every action.
        """
        target = f"{event.namespace or 'default'}/{event.resource_name or 'unknown'}"
        runbook_id = runbook.id

        # Pre-checks
        pre_ok = await self._run_pre_checks(runbook, event)
        if not pre_ok:
            logger.info(
                f"[RunbookExecutor] Pre-checks FAILED for {runbook_id} — skipping"
            )
            return

        # Per-action governance loop
        action_results: list[dict] = []
        execution_failed = False
        action_id: str | None = None
        pre_state = None

        for action in runbook.actions:
            # ActionLadder evaluation
            decision = await self.ladder.evaluate(
                runbook=runbook,
                action=action,
                event=event,
                target=target,
                confidence=self.confidence,
            )

            if decision.requires_approval:
                logger.warning(
                    f"[RunbookExecutor] L3 action STAGED for approval "
                    f"(approval_id={decision.approval_id}) — "
                    f"skipping remaining actions in {runbook_id}"
                )
                action_results.append(
                    {
                        "type": action.type,
                        "status": "pending_approval",
                        "approval_id": decision.approval_id,
                    }
                )
                break

            if not decision.can_proceed:
                logger.info(
                    f"[RunbookExecutor] Action BLOCKED by governance: "
                    f"{action.type} — {decision.denial_reason}"
                )
                action_results.append(
                    {
                        "type": action.type,
                        "status": "governance_blocked",
                        "reason": decision.denial_reason,
                    }
                )
                if action.abort_on_failure:
                    execution_failed = True
                    break
                continue

            # Pre-state capture
            self._ensure_k8s()
            pre_state = await self.rollback_reg.capture(
                action_type=action.type,
                namespace=event.namespace or "default",
                name=event.resource_name or "",
                k8s_apps=self._k8s_apps,
                k8s_core=self._k8s_core,
            )

            # Write pending audit record (crash-safe)
            action_id = await self.audit.write_pending(
                triggered_by="runbook_executor_v3",
                runbook_id=runbook_id,
                healing_level=runbook.healing_level,
                target=target,
                pre_check_results={"passed": True},
                incident_id=event.correlation_id or event.event_id,
                action_id=None,  # generate new UUID
            )

            # Execute action
            result = await self._execute_action(action, event)
            action_results.append(result)

            if result["status"] == "failed" and action.abort_on_failure:
                await self.audit.update_outcome(
                    action_id,
                    execution_outcome="failed",
                    action_results=action_results,
                )
                execution_failed = True
                break

            # Update audit (intermediate — will be overwritten at post-check)
            await self.audit.update_outcome(
                action_id,
                execution_outcome="executed",
                action_results=action_results,
            )

        if execution_failed:
            logger.error(
                f"[RunbookExecutor] Runbook {runbook_id} ABORTED: action failed"
            )
            return

        # Post-checks
        post_ok = await self._run_post_checks(runbook)
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
                if len(runbook.actions) > 0 and pre_state is not None:
                    rb_infra = await self.rollback_reg.rollback(
                        pre_state, self._k8s_apps, self._k8s_core
                    )
                    action_results.append(rb_infra)

                rollback_done = True
        else:
            self.ladder.record_post_check_success()

        # Finalise audit record
        outcome = (
            "success" if post_ok else ("rolled_back" if rollback_done else "failed")
        )
        if action_id is not None:
            await self.audit.update_outcome(
                action_id,
                execution_outcome=outcome,
                post_check_results={"slo_restored": post_ok},
                rollback_triggered=rollback_done,
                action_results=action_results,
            )

        # Set cooldown (only on non-failure outcomes)
        if outcome in ("success", "rolled_back"):
            await self.ladder.set_cooldown(runbook, target)

        logger.info(
            f"[RunbookExecutor] Runbook {runbook_id} COMPLETE — "
            f"outcome={outcome} target={target}"
        )

        await self._publish_action_result(
            action_id=action_id,
            runbook=runbook,
            event=event,
            target=target,
            outcome=outcome,
            rollback_done=rollback_done,
            action_results=action_results,
            post_ok=post_ok,
        )

    async def _publish_action_result(
        self,
        *,
        action_id: str,
        runbook: Runbook,
        event: IncidentEvent,
        target: str,
        outcome: str,
        rollback_done: bool,
        action_results: list[dict],
        post_ok: bool,
    ) -> None:
        """Publish the final action result to nexus.actions.* for Notifier pickup.

        Subject: nexus.actions.<runbook_id>.<outcome>
        Payload mirrors RunbookExecutor result + audit row so Notifier / dashboards
        have full context without joining to the AuditTrail.
        """
        subject = f"nexus.actions.{runbook.id}.{outcome}"
        payload = {
            "type": "healing_action",
            "action_id": action_id,
            "runbook_id": runbook.id,
            "healing_level": runbook.healing_level,
            "target": target,
            "namespace": event.namespace,
            "resource_name": event.resource_name,
            "incident_id": event.correlation_id or event.event_id,
            "signal_type": str(event.signal_type),
            "outcome": outcome,
            "description": runbook.description,
            "rollback_triggered": rollback_done,
            "post_check_passed": post_ok,
            "action_results": action_results,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            js = getattr(self.nats, "_js", None)
            if js is not None:
                await js.publish(subject, json.dumps(payload).encode())
                logger.debug(f"[RunbookExecutor] Published action event to {subject}")
        except Exception as exc:
            # Notifications are best-effort — never break healing on publish failure
            logger.debug(f"[RunbookExecutor] action publish failed: {exc}")

    # Main event handler
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

    # NOTE: a NATS-driven ``start()`` was removed here (was previously at this
    # location). The orchestrator is the sole NATS consumer in production and
    # invokes ``handle_event()`` directly. Calling both paths would double-fire
    # every runbook.

# Factory — build a fully-configured executor from env + defaults
def build_executor(
    nats_client: NATSClient,
    runbook_dir: Path,
    audit_trail: AuditTrail,
    prometheus_url: str = "http://prometheus:9090",
    opa_url: str = "http://localhost:8181",
    db_path: str | None = None,
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

    cooldown = CooldownStore(db_path=db_path)
    approval = HumanApprovalQueue(nats_client=nats_client)
    cb = GovernanceCircuitBreaker(failure_threshold=3)
    policy = PolicyEngine(opa_url=opa_url)
    ladder = ActionLadder(policy, cooldown, approval, cb)
    rollback = RollbackRegistry(dry_run=dry_run)

    return RunbookExecutor(
        nats_client=nats_client,
        runbook_dir=runbook_dir,
        audit_trail=audit_trail,
        action_ladder=ladder,
        rollback_registry=rollback,
        prometheus_url=prometheus_url,
        dry_run=dry_run,
        confidence=confidence,
    )
