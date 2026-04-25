"""
NEXUS Runbook Advisor
======================
Analyzes historical healing outcomes from the OutcomeStore and generates
actionable recommendations for improving the runbook library.

Recommendation triggers and their thresholds:

    FLAG_FOR_REVIEW     success_rate < 0.50, n ≥ 5
        The runbook is failing more than it succeeds.
        Likely cause: wrong pre-check logic, too-aggressive action, or
        the runbook addresses the symptom not the root cause.

    ADD_PRE_CHECK       success_rate 0.50–0.70, n ≥ 5
        The runbook sometimes works — adding pre-checks (e.g. confirm
        the deployment is actually unhealthy before restarting) would
        reduce false positives.

    REDUCE_BLAST_RADIUS rollback_rate > 0.30, n ≥ 5
        More than 30% of successful executions needed rollback, suggesting
        the action is causing side effects. Consider narrower scope.

    PROMOTE_CONFIDENCE  success_rate ≥ 0.90, n ≥ 20
        This runbook consistently heals successfully. The ConfidenceScorer
        already boosts it via KnowledgeBase — but this recommendation
        surfaces it for possible auto-approval promotion.

    CHRONIC_TARGET      target receives > 3 heals/day for same runbook
        The root cause is not being fixed, only the symptoms.
        A human engineer should investigate the deployment.

    INVESTIGATE_DEGRADATION  time-series shows declining success_rate
        Recent 7-day rate is significantly lower than 30-day rate.
        Something changed — new code, new traffic pattern, infra change.

All recommendations are logged as structured JSON and returned to the
FeedbackLoop for NATS publication (surfaced in Phase 7 dashboard).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nexus.learning.outcome_store import OutcomeStore, RunbookStats, SystemKPIs

logger = logging.getLogger(__name__)

# Thresholds
_FLAG_RATE         = 0.50
_ADD_PRECHECK_RATE = 0.70
_HIGH_ROLLBACK     = 0.30
_EXCELLENT_RATE    = 0.90
_MIN_SAMPLES       = 5
_EXCELLENT_MIN_N   = 20
_CHRONIC_HEALS_PER_DAY = 3

# Severity levels
SEV_INFO     = "info"
SEV_WARN     = "warning"
SEV_ACTION   = "action_required"


@dataclass
class RunbookRecommendation:
    """A single advisor recommendation for one runbook."""
    runbook_id:       str
    severity:         str
    recommendation:   str
    message:          str
    suggested_action: str
    evidence:         Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runbook_id":       self.runbook_id,
            "severity":         self.severity,
            "recommendation":   self.recommendation,
            "message":          self.message,
            "suggested_action": self.suggested_action,
            "evidence":         self.evidence,
        }

    def __str__(self) -> str:
        return (
            f"[{self.severity.upper()}] {self.runbook_id}: "
            f"{self.recommendation} — {self.suggested_action}"
        )


class RunbookAdvisor:
    """
    Stateless analyzer that converts RunbookStats → RunbookRecommendations.

    Args:
        outcome_store: Used for time-series queries (7-day vs 30-day comparison).
    """

    def __init__(self, outcome_store: Optional[OutcomeStore] = None):
        self._store = outcome_store

    def analyze(
        self,
        all_stats:  Dict[str, RunbookStats],
        system_kpis: Optional[SystemKPIs] = None,
    ) -> List[RunbookRecommendation]:
        """
        Run all advisory rules against the current runbook statistics.
        Returns a list of recommendations, sorted by severity (action_required first).
        """
        recs: List[RunbookRecommendation] = []

        for rb_id, stats in all_stats.items():
            recs.extend(self._check_runbook(stats))

        if system_kpis:
            recs.extend(self._check_system(system_kpis))

        # Sort: action_required > warning > info
        order = {SEV_ACTION: 0, SEV_WARN: 1, SEV_INFO: 2}
        recs.sort(key=lambda r: order.get(r.severity, 3))

        if recs:
            logger.warning(
                f"[RunbookAdvisor] {len(recs)} recommendation(s): "
                + ", ".join(str(r) for r in recs[:3])
                + ("..." if len(recs) > 3 else "")
            )
        else:
            logger.debug("[RunbookAdvisor] No recommendations — all runbooks healthy")

        return recs

    # ── Per-runbook rules ─────────────────────────────────────────────────────

    def _check_runbook(self, stats: RunbookStats) -> List[RunbookRecommendation]:
        recs: List[RunbookRecommendation] = []
        n     = stats.completed
        rate  = stats.success_rate
        rb_id = stats.runbook_id

        if n < _MIN_SAMPLES:
            return []   # Not enough data

        # 1. FLAG_FOR_REVIEW — failing majority of the time
        if rate < _FLAG_RATE:
            recs.append(RunbookRecommendation(
                runbook_id       = rb_id,
                severity         = SEV_ACTION,
                recommendation   = "FLAG_FOR_REVIEW",
                message          = (
                    f"Success rate {rate:.0%} across {n} executions — "
                    f"this runbook fails more often than it heals. "
                    f"Review action logic and pre-check conditions."
                ),
                suggested_action = "Disable autonomous execution; audit runbook YAML actions",
                evidence         = stats.to_dict(),
            ))
            return recs   # Don't pile on more recs for the same runbook

        # 2. ADD_PRE_CHECK — succeeds sometimes but not reliably
        if _FLAG_RATE <= rate < _ADD_PRECHECK_RATE:
            recs.append(RunbookRecommendation(
                runbook_id       = rb_id,
                severity         = SEV_WARN,
                recommendation   = "ADD_PRE_CHECK",
                message          = (
                    f"Success rate {rate:.0%} — inconsistent healing. "
                    f"Adding pre-checks (e.g. confirm pod is actually unhealthy "
                    f"before restarting) would reduce false-start executions."
                ),
                suggested_action = (
                    "Add pre_checks to runbook YAML: "
                    "pod_is_crashlooping / error_rate_confirmed / deployment_degraded"
                ),
                evidence         = stats.to_dict(),
            ))

        # 3. REDUCE_BLAST_RADIUS — rollback rate high even when healing succeeds
        if stats.rollback_rate > _HIGH_ROLLBACK:
            recs.append(RunbookRecommendation(
                runbook_id       = rb_id,
                severity         = SEV_WARN,
                recommendation   = "REDUCE_BLAST_RADIUS",
                message          = (
                    f"Rollback rate {stats.rollback_rate:.0%} — "
                    f"the action is causing side effects that post-checks are catching. "
                    f"Consider narrowing scope or adding rollback hooks."
                ),
                suggested_action = (
                    "Scope action to single replica first; "
                    "add pod-count post_check to detect cascading restarts"
                ),
                evidence         = {
                    "rollback_count": stats.rolled_back,
                    "rollback_rate":  round(stats.rollback_rate, 3),
                    **stats.to_dict(),
                },
            ))

        # 4. PROMOTE_CONFIDENCE — excellent performer
        if rate >= _EXCELLENT_RATE and n >= _EXCELLENT_MIN_N:
            recs.append(RunbookRecommendation(
                runbook_id       = rb_id,
                severity         = SEV_INFO,
                recommendation   = "PROMOTE_CONFIDENCE",
                message          = (
                    f"Excellent: {rate:.0%} success rate across {n} executions. "
                    f"KnowledgeBase +{0.05:.2f} confidence boost already applied. "
                    f"Eligible for auto-approve at L2 without human review."
                ),
                suggested_action = (
                    "Promote healing_level cap in OPA policy: "
                    f"allow_auto_approve[\"{rb_id}\"] = true"
                ),
                evidence         = stats.to_dict(),
            ))

        return recs

    # ── System-level rules ────────────────────────────────────────────────────

    def _check_system(self, kpis: SystemKPIs) -> List[RunbookRecommendation]:
        recs: List[RunbookRecommendation] = []

        # High false-heal rate across all runbooks
        if kpis.false_heal_rate > 0.40 and kpis.total_actions >= 10:
            recs.append(RunbookRecommendation(
                runbook_id       = "SYSTEM",
                severity         = SEV_ACTION,
                recommendation   = "HIGH_SYSTEM_FALSE_HEAL_RATE",
                message          = (
                    f"System-wide false-heal rate is {kpis.false_heal_rate:.0%} — "
                    f"autonomous healing is causing more harm than good. "
                    f"Consider halting autonomous L2/L3 actions and require human approval."
                ),
                suggested_action = (
                    "Set GovernanceCircuitBreaker threshold to 1 failure; "
                    "review all runbooks with success_rate < 0.70"
                ),
                evidence         = kpis.to_dict(),
            ))

        # Good overall performance — log for operators
        if kpis.autonomous_success_rate >= 0.85 and kpis.total_actions >= 20:
            recs.append(RunbookRecommendation(
                runbook_id       = "SYSTEM",
                severity         = SEV_INFO,
                recommendation   = "SYSTEM_PERFORMING_WELL",
                message          = (
                    f"Autonomous healing success rate: {kpis.autonomous_success_rate:.0%} "
                    f"across {kpis.total_actions} actions in {kpis.window_days} days."
                ),
                suggested_action = "Continue monitoring; review Prescaler graduation criteria",
                evidence         = kpis.to_dict(),
            ))

        return recs

    # ── Async queries for time-series analysis ────────────────────────────────

    async def find_chronic_targets(self) -> List[RunbookRecommendation]:
        """
        Identify resources that keep needing the same healing action.
        These are permanent failures that runbooks alone cannot fix.
        """
        if not self._store:
            return []

        rows = await self._store.get_targets_with_most_heals(days=7)
        recs = []
        for row in rows:
            daily_rate = row["heal_count"] / 7.0
            if daily_rate >= _CHRONIC_HEALS_PER_DAY:
                recs.append(RunbookRecommendation(
                    runbook_id       = row["runbook_id"],
                    severity         = SEV_ACTION,
                    recommendation   = "CHRONIC_TARGET",
                    message          = (
                        f"Target {row['target']} received {row['heal_count']} heals "
                        f"in 7 days ({daily_rate:.1f}/day) with runbook {row['runbook_id']}. "
                        f"Root cause is not being fixed — only symptoms."
                    ),
                    suggested_action = (
                        f"Escalate {row['target']} for human engineering review. "
                        f"Increase cooldown on {row['runbook_id']} to 1h to force investigation."
                    ),
                    evidence         = dict(row),
                ))
        return recs
