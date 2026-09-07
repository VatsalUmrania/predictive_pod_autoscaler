"""Unit tests for NEXUS incident deduplication, FSM retry loops, and escalation across K8s and AWS."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bus.incident_event import AgentType, IncidentEvent, Severity, SignalType
from nexus.db.postgres import SQLiteFallbackClient
from nexus.engine.fsm import IncidentState
from nexus.governance.action_ladder import HumanApprovalQueue
from nexus.governance.cooldown_store import CooldownStore
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.incident_cluster import IncidentCluster
from nexus.reasoning.orchestrator import NexusOrchestrator
from nexus.reasoning.rca_engine import RCAEngine, RCAResult


def build_mock_executor(mock_nats, db):
    ladder = MagicMock()
    queue = HumanApprovalQueue(nats_client=mock_nats)
    ladder.approval_queue = queue
    ladder._cooldown = CooldownStore(db_path=":memory:")
    ladder.evaluate = AsyncMock()
    ladder.record_post_check_success = MagicMock()
    ladder.record_post_check_failure = MagicMock()

    executor = MagicMock(spec=RunbookExecutor)
    executor.nats = mock_nats
    executor.db_client = db
    executor.ladder = ladder
    executor.library = MagicMock()
    executor.library.get.return_value = None
    executor.handle_event = AsyncMock()
    executor.on_incident_resolved = None
    executor.on_incident_unsolved = None
    return executor


@pytest.mark.asyncio
async def test_k8s_incident_dedup_and_retry_loop():
    """Verify that multiple events for a K8s resource reuse the same incident ID and handle retries."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    executor = build_mock_executor(mock_nats, db)

    rca = MagicMock(spec=RCAEngine)
    rca.analyze = AsyncMock(return_value=RCAResult(
        failure_class="pod_crashloop",
        root_cause="OOM error in container",
        reasoning="Memory limit exceeded",
        healing_level=1,
        confidence=0.9,
        runbook_id="runbook_pod_crashloop_v1",
        source="rule_based",
    ))
    scorer = MagicMock(spec=ConfidenceScorer)
    scorer.score.return_value = 0.9
    scorer.gate.return_value = 1
    scorer.describe.return_value = "high"

    orchestrator = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=EventCorrelator(),
        rca_engine=rca,
        confidence_scorer=scorer,
        executor=executor,
        db_client=db,
    )

    # 1. First event for nexus/opa
    event1 = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_CRASHLOOP,
        severity=Severity.CRITICAL,
        namespace="nexus",
        resource_name="opa",
    )
    cluster1 = IncidentCluster.new(event1)
    await orchestrator._process_cluster(cluster1)

    target = "nexus/opa"
    assert target in orchestrator._active_incidents
    active = orchestrator._active_incidents[target]
    incident_id = active["incident_id"]
    assert active["environment"] == "kubernetes"
    assert active["retry_count"] == 0

    # 2. Second event arrives while incident is in flight (e.g. EXECUTING)
    active["fsm"]._current_state = IncidentState.EXECUTING
    cluster2 = IncidentCluster.new(event1)
    await orchestrator._process_cluster(cluster2)

    # Must NOT create a new incident or dispatch a second action
    assert orchestrator._active_incidents[target]["incident_id"] == incident_id
    assert rca.analyze.await_count == 1  # Analysis was suppressed

    # 3. Simulate post-check failure on execution -> transitions to RETRYING
    orchestrator.notify_incident_unsolved(target, incident_id)
    assert active["fsm"].current_state == IncidentState.RETRYING

    # 4. Third event arrives during RETRYING state -> initiates retry under SAME incident ID
    await orchestrator._process_cluster(cluster2)
    assert orchestrator._active_incidents[target]["incident_id"] == incident_id
    assert active["retry_count"] == 1
    assert rca.analyze.await_count == 2  # RCA re-run for retry

    # 5. Resolve incident
    orchestrator.notify_incident_resolved(target, incident_id)
    assert target not in orchestrator._active_incidents

    await db.close()


@pytest.mark.asyncio
async def test_aws_incident_escalation_after_max_retries():
    """Verify that AWS serverless incidents escalate when max_retries is exceeded."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    executor = build_mock_executor(mock_nats, db)

    rca = MagicMock(spec=RCAEngine)
    rca.analyze = AsyncMock(return_value=RCAResult(
        failure_class="lambda_timeout",
        root_cause="Downstream API latency spike",
        reasoning="Timeout breached",
        healing_level=1,
        confidence=0.85,
        runbook_id="runbook_lambda_timeout_v1",
        source="rule_based",
    ))
    scorer = MagicMock(spec=ConfidenceScorer)
    scorer.score.return_value = 0.85
    scorer.gate.return_value = 1
    scorer.describe.return_value = "high"

    orchestrator = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=EventCorrelator(),
        rca_engine=rca,
        confidence_scorer=scorer,
        executor=executor,
        db_client=db,
    )

    aws_target = "us-east-1/order-processor"
    aws_event = IncidentEvent(
        agent=AgentType.LAMBDA,
        signal_type=SignalType.LAMBDA_TIMEOUT,
        severity=Severity.CRITICAL,
        namespace="us-east-1",
        resource_name="order-processor",
    )
    cluster = IncidentCluster.new(aws_event)

    # Initial processing
    await orchestrator._process_cluster(cluster)
    active = orchestrator._active_incidents[aws_target]
    assert active["environment"] == "aws"
    incident_id = active["incident_id"]

    # Fail post-checks 3 times
    for attempt in range(1, 4):
        orchestrator.notify_incident_unsolved(aws_target, incident_id)
        assert active["fsm"].current_state == IncidentState.RETRYING
        await orchestrator._process_cluster(cluster)
        assert active["retry_count"] == attempt

    # 4th failure triggers escalation
    orchestrator.notify_incident_unsolved(aws_target, incident_id)
    await orchestrator._process_cluster(cluster)

    assert active["fsm"].current_state == IncidentState.ESCALATED
    # Escalation alert published to NATS
    mock_nats.publish_raw.assert_awaited()
    subjects = [call.args[0] for call in mock_nats.publish_raw.await_args_list]
    assert "nexus.alerts.escalated" in subjects

    await db.close()


@pytest.mark.asyncio
async def test_execute_approved_fsm_retry_and_resolved():
    """Verify execute_approved transitions IncidentFSM to RETRYING on post-check failure and RESOLVED on success."""
    from nexus.governance.action_ladder import PendingApproval
    from nexus.governance.action_ladder import LadderDecision
    from nexus.governance.policy_engine import PolicyDecision

    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    inc_id = await db.create_incident(
        fingerprint="fp-exec-1",
        environment="kubernetes",
        target_resource="nexus/opa",
    )

    audit = MagicMock()
    audit.write_pending = AsyncMock(return_value="act-1")
    audit.update_outcome = AsyncMock()

    ladder = MagicMock()
    ladder.evaluate = AsyncMock(return_value=LadderDecision(can_proceed=True, policy_decision=PolicyDecision(allowed=True)))
    cooldown = CooldownStore(db_path=":memory:")
    ladder._cooldown = cooldown
    ladder.set_cooldown = AsyncMock()
    ladder.record_post_check_success = MagicMock()
    ladder.record_post_check_failure = MagicMock()

    rollback = MagicMock()
    rollback.capture = AsyncMock()

    library = MagicMock()
    runbook_mock = MagicMock()
    runbook_mock.id = "runbook_pod_crashloop_v1"
    runbook_mock.healing_level = 2
    library.get.return_value = runbook_mock

    executor = RunbookExecutor(
        nats_client=mock_nats,
        audit_trail=audit,
        action_ladder=ladder,
        rollback_registry=rollback,
        library=library,
        db_client=db,
    )
    executor._ensure_k8s = MagicMock()
    executor._execute_action = AsyncMock(return_value={"status": "executed"})

    unsolved_called = []
    resolved_called = []
    executor.on_incident_unsolved = lambda target, iid: unsolved_called.append((target, iid))
    executor.on_incident_resolved = lambda target, iid: resolved_called.append((target, iid))

    pending = PendingApproval(
        approval_id="TESTAPP1",
        runbook_id="runbook_pod_crashloop_v1",
        action_type="restart_pod",
        target="nexus/opa",
        incident_id=inc_id,
        healing_level=2,
        confidence=0.8,
        enqueued_at="2026-09-04T15:00:00Z",
        context={
            "action": {"type": "restart_pod", "params": {"namespace": "nexus", "name": "opa"}},
            "event": {
                "agent": "k8s",
                "signal_type": "pod_crashloop",
                "severity": "critical",
                "namespace": "nexus",
                "resource_name": "opa",
            },
        },
    )

    # 1. Post-checks FAIL -> outcome = failed, FSM -> retrying, cooldown set
    executor._run_post_checks = AsyncMock(return_value=False)
    res_fail = await executor.execute_approved(pending)
    assert res_fail["status"] == "failed"
    assert len(unsolved_called) == 1
    assert unsolved_called[0] == ("nexus/opa", inc_id)
    assert await cooldown.is_in_cooldown(CooldownStore.make_key("runbook_pod_crashloop_v1", "nexus/opa")) is True

    inc_row = await db.get_incident(inc_id)
    assert inc_row["current_state"] == "retrying"

    # 2. Post-checks PASS -> outcome = success, FSM -> resolved
    executor._run_post_checks = AsyncMock(return_value=True)
    res_ok = await executor.execute_approved(pending)
    assert res_ok["status"] == "success"
    assert len(resolved_called) == 1
    assert resolved_called[0] == ("nexus/opa", inc_id)

    inc_row_resolved = await db.get_incident(inc_id)
    assert inc_row_resolved["current_state"] == "resolved"

    await db.close()


@pytest.mark.asyncio
async def test_incident_cluster_fingerprint_and_db_auto_ensure():
    """Verify IncidentCluster has fingerprint and DB transition_state auto-ensures parent row."""
    event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.DEPLOYMENT_DEGRADED,
        severity=Severity.CRITICAL,
        namespace="monitoring",
        resource_name="prometheus-grafana",
    )
    cluster = IncidentCluster.new(event)
    assert cluster.fingerprint == "monitoring:prometheus-grafana"

    # Test DB transition_state without prior create_incident (should auto-create stub, no crash)
    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    orphan_inc_id = "1582ceec-5d10-4c27-b524-0d5f8f960ee2"
    await db.transition_state(
        incident_id=orphan_inc_id,
        to_state="correlated",
        reason="Test auto ensure",
    )

    inc = await db.get_incident(orphan_inc_id)
    assert inc is not None
    assert inc["current_state"] == "correlated"

    # Further transition
    await db.transition_state(
        incident_id=orphan_inc_id,
        to_state="diagnosing",
        reason="Starting RCA",
    )
    inc_after = await db.get_incident(orphan_inc_id)
    assert inc_after["current_state"] == "diagnosing"

    await db.close()


@pytest.mark.asyncio
async def test_concurrent_cluster_arrival_signal_consolidation():
    """Verify that two clusters for the same target arriving in rapid succession do not duplicate incidents."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    executor = build_mock_executor(mock_nats, db)
    rca = MagicMock(spec=RCAEngine)
    rca.analyze = AsyncMock(return_value=RCAResult(
        failure_class="pod_crashloop",
        root_cause="CrashLoopBackOff",
        reasoning="Container died",
        healing_level=1,
        confidence=0.8,
        runbook_id="runbook_pod_crashloop_v1",
        source="rule_based",
    ))
    scorer = MagicMock(spec=ConfidenceScorer)
    scorer.score.return_value = 0.8
    scorer.gate.return_value = 1
    scorer.describe.return_value = "high"

    orchestrator = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=EventCorrelator(),
        rca_engine=rca,
        confidence_scorer=scorer,
        executor=executor,
        db_client=db,
    )

    event1 = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.DEPLOYMENT_DEGRADED,
        severity=Severity.CRITICAL,
        namespace="monitoring",
        resource_name="prometheus-grafana",
    )
    event2 = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_PENDING,
        severity=Severity.CRITICAL,
        namespace="monitoring",
        resource_name="prometheus-grafana",
    )
    cluster1 = IncidentCluster.new(event1)
    cluster2 = IncidentCluster.new(event2)

    # Process both concurrently
    await asyncio.gather(
        orchestrator._process_cluster(cluster1),
        orchestrator._process_cluster(cluster2),
    )

    target = "monitoring/prometheus-grafana"
    assert target in orchestrator._active_incidents
    # Only ONE incident should exist in active incidents
    assert len(orchestrator._active_incidents) == 1

    await db.close()


def test_gemini_provider_thread_safe_init():
    """Verify GeminiProvider initializes client thread-safely without error."""
    import concurrent.futures
    from unittest.mock import patch
    from nexus.reasoning.llm_provider import GeminiProvider

    provider = GeminiProvider(api_key="test-api-key")
    mock_genai_client = MagicMock()

    with patch("google.genai.Client", return_value=mock_genai_client) as mock_client_cls:
        def init_client():
            return provider._ensure_client()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(init_client) for _ in range(10)]
            results = [f.result() for f in futures]

        assert all(results)
        # Client constructor called exactly once despite 10 concurrent calls
        assert mock_client_cls.call_count == 1


@pytest.mark.asyncio
async def test_llm_rca_blocked_falls_back_to_rule_based_rca():
    """Verify that when LLM hallucinates an unsupported diagnosis (blocked by validator),
    the orchestrator falls back to deterministic rule-based RCA instead of giving up."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    executor = build_mock_executor(mock_nats, db)

    # LLM hallucinates resource_exhaustion from pod_crashloop alone
    rca = MagicMock(spec=RCAEngine)
    rca.analyze = AsyncMock(return_value=RCAResult(
        failure_class="resource_exhaustion",
        root_cause="Process failed during startup due to memory constraints",
        reasoning="Pod is in CrashLoopBackOff",
        healing_level=1,
        confidence=0.8,
        runbook_id="runbook_pod_crashloop_v1",
        source="gemini",
    ))
    scorer = MagicMock(spec=ConfidenceScorer)
    scorer.score.return_value = 0.82
    scorer.gate.return_value = 1
    scorer.describe.return_value = "high"

    orchestrator = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=EventCorrelator(),
        rca_engine=rca,
        confidence_scorer=scorer,
        executor=executor,
        db_client=db,
    )

    event = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_CRASHLOOP,
        severity=Severity.CRITICAL,
        namespace="default",
        resource_name="shop-demo",
    )
    cluster = IncidentCluster.new(event)
    await orchestrator._process_cluster(cluster)

    # Verify that rule fallback took over:
    # Rule 4 matches pod_crashloop -> bad_deploy, runbook_high_error_rate_post_deploy_v1
    assert executor.handle_event.await_count == 1
    called_event = executor.handle_event.call_args[0][0]
    assert called_event.suggested_runbook == "runbook_high_error_rate_post_deploy_v1"
    assert called_event.context["_rca"]["failure_class"] == "bad_deploy"
    assert called_event.context["_rca"]["source"] == "rule_based"

    await db.close()


@pytest.mark.asyncio
async def test_undispatched_incident_clears_active_state_preventing_deadlock():
    """Verify that when an incident produces no autonomous action (L0 / no runbook),
    _active_incidents is cleared so subsequent events for the target are not deadlocked."""
    mock_nats = MagicMock()
    mock_nats.publish_raw = AsyncMock()
    mock_nats.publish = AsyncMock()

    db = SQLiteFallbackClient(db_path=":memory:")
    await db.initialize()

    executor = build_mock_executor(mock_nats, db)

    # RCA returns unknown / L0 / no runbook
    rca = MagicMock(spec=RCAEngine)
    rca.analyze = AsyncMock(return_value=RCAResult(
        failure_class="unknown",
        root_cause="Unknown issue",
        reasoning="Cannot diagnose",
        healing_level=0,
        confidence=0.3,
        runbook_id=None,
        source="rule_based",
    ))
    scorer = MagicMock(spec=ConfidenceScorer)
    scorer.score.return_value = 0.3
    scorer.gate.return_value = 0
    scorer.describe.return_value = "low"

    orchestrator = NexusOrchestrator(
        nats_client=mock_nats,
        correlator=EventCorrelator(),
        rca_engine=rca,
        confidence_scorer=scorer,
        executor=executor,
        db_client=db,
    )

    event1 = IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.POD_PENDING,
        severity=Severity.WARNING,
        namespace="default",
        resource_name="shop-demo",
    )
    cluster1 = IncidentCluster.new(event1)
    await orchestrator._process_cluster(cluster1)

    # Active incidents must be cleared, not stuck in POLICY_CHECK
    target = "default/shop-demo"
    assert target not in orchestrator._active_incidents

    # Second cluster for shop-demo must NOT be suppressed as duplicate
    cluster2 = IncidentCluster.new(event1)
    await orchestrator._process_cluster(cluster2)
    assert rca.analyze.await_count == 2  # Analyzed again, not suppressed

    await db.close()



