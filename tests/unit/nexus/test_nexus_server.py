"""Unit tests for NexusServer.create() / start() / stop()."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.server import NexusServer


def _awaitable_magicmock() -> MagicMock:
    """Return a MagicMock whose async methods are awaitable.

    NexusServer.create() awaits many methods (AuditTrail.initialize(),
    PpaOutcomeTracker.start(), nats.subscribe_raw(), nats.publish(),
    feedback_loop.start(), feedback_loop.stop(), ...) — without AsyncMock
    attributes the test breaks on
    'object MagicMock can't be used in await expression'.
    """
    m = MagicMock()
    m.initialize = AsyncMock()
    m.connect = AsyncMock()
    m.subscribe_raw = AsyncMock()
    m.publish = AsyncMock()
    m.start = AsyncMock()
    m.stop = AsyncMock()
    return m


def test_nexus_server_has_create_classmethod():
    assert hasattr(NexusServer, "create")
    assert callable(NexusServer.create)


@pytest.mark.asyncio
async def test_create_wires_all_components():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    mock_nats.close = AsyncMock()
    mock_nats.subscribe_raw = AsyncMock()

    mock_prescaler = MagicMock()
    mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

    mock_tracker = MagicMock()
    mock_tracker.start = AsyncMock()
    mock_tracker.stop = AsyncMock()

    mock_agent_manager = MagicMock()
    mock_agent_manager.start = AsyncMock()
    mock_agent_manager.stop = MagicMock()
    mock_agent_manager._tasks = []

    mock_notifier = MagicMock()
    mock_notifier.start_background = MagicMock()
    mock_notifier.stop = MagicMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.start = AsyncMock()
    mock_orchestrator.stop = AsyncMock()

    mock_rca_engine = MagicMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        with patch("nexus.server.Prescaler", return_value=mock_prescaler):
            with patch("nexus.server.PpaOutcomeTracker", return_value=mock_tracker):
                with patch(
                    "nexus.server.AgentManager", return_value=mock_agent_manager
                ):
                    with patch("nexus.server.Notifier", return_value=mock_notifier):
                        with patch(
                            "nexus.server.NexusOrchestrator",
                            return_value=mock_orchestrator,
                        ):
                            with patch(
                                "nexus.server.RCAEngine", return_value=mock_rca_engine
                            ):
                                with patch(
                                    "nexus.server.AuditTrail",
                                    return_value=_awaitable_magicmock(),
                                ):
                                    with patch(
                                        "nexus.server.RollbackRegistry",
                                        return_value=MagicMock(),
                                    ):
                                        with patch(
                                            "nexus.server.PolicyEngine",
                                            return_value=_awaitable_magicmock(),
                                        ):
                                            with patch(
                                                "nexus.server.CooldownStore",
                                                return_value=_awaitable_magicmock(),
                                            ):
                                                with patch(
                                                    "nexus.server.GovernanceCircuitBreaker",
                                                    return_value=_awaitable_magicmock(),
                                                ):
                                                    with patch(
                                                        "nexus.server.HumanApprovalQueue",
                                                        return_value=MagicMock(),
                                                    ):
                                                        with patch(
                                                            "nexus.server.RunbookExecutor",
                                                            return_value=MagicMock(),
                                                        ):
                                                            with patch(
                                                                "nexus.server.OutcomeStore",
                                                                return_value=_awaitable_magicmock(),
                                                            ):
                                                                with patch(
                                                                    "nexus.server.KnowledgeBase",
                                                                    return_value=_awaitable_magicmock(),
                                                                ):
                                                                    with patch(
                                                                        "nexus.server.build_feedback_loop",
                                                                        new=AsyncMock(
                                                                            return_value=_awaitable_magicmock()
                                                                        ),
                                                                    ):
                                                                        server = await NexusServer.create(
                                                                            nats_url="nats://mock:4222"
                                                                        )

        assert server.nats_client is mock_nats
        assert server.prescaler is mock_prescaler
        assert server.outcome_tracker is mock_tracker
        assert server.agent_manager is mock_agent_manager
        assert server.notifier is mock_notifier
        assert server.orchestrator is mock_orchestrator

        mock_notifier.start_background.assert_called_once_with(mock_nats)


@pytest.mark.asyncio
async def test_create_does_not_call_outcome_tracker_start():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    mock_nats.subscribe_raw = AsyncMock()

    mock_tracker = MagicMock()
    mock_tracker.start = MagicMock()
    mock_tracker.stop = AsyncMock()

    mock_prescaler = MagicMock()
    mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.start = AsyncMock()
    mock_orchestrator.stop = AsyncMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        with patch("nexus.server.PpaOutcomeTracker", return_value=mock_tracker):
            with patch("nexus.server.Prescaler", return_value=mock_prescaler):
                with patch(
                    "nexus.server.NexusOrchestrator", return_value=mock_orchestrator
                ):
                    with patch("nexus.server.Notifier") as mock_notifier_cls:
                        mock_notifier_cls.return_value.start_background = MagicMock()
                        mock_notifier_cls.return_value.stop = MagicMock()
                        with patch(
                            "nexus.server.AgentManager", return_value=MagicMock()
                        ):
                            with patch(
                                "nexus.server.RCAEngine", return_value=MagicMock()
                            ):
                                with patch(
                                    "nexus.server.AuditTrail",
                                    return_value=_awaitable_magicmock(),
                                ):
                                    with patch(
                                        "nexus.server.RollbackRegistry",
                                        return_value=MagicMock(),
                                    ):
                                        with patch(
                                            "nexus.server.PolicyEngine",
                                            return_value=_awaitable_magicmock(),
                                        ):
                                            with patch(
                                                "nexus.server.CooldownStore",
                                                return_value=_awaitable_magicmock(),
                                            ):
                                                with patch(
                                                    "nexus.server.GovernanceCircuitBreaker",
                                                    return_value=_awaitable_magicmock(),
                                                ):
                                                    with patch(
                                                        "nexus.server.HumanApprovalQueue",
                                                        return_value=MagicMock(),
                                                    ):
                                                        with patch(
                                                            "nexus.server.RunbookExecutor",
                                                            return_value=MagicMock(),
                                                        ):
                                                            with patch(
                                                                "nexus.server.OutcomeStore",
                                                                return_value=_awaitable_magicmock(),
                                                            ):
                                                                with patch(
                                                                    "nexus.server.KnowledgeBase",
                                                                    return_value=_awaitable_magicmock(),
                                                                ):
                                                                    with patch(
                                                                        "nexus.server.build_feedback_loop",
                                                                        new=AsyncMock(
                                                                            return_value=_awaitable_magicmock()
                                                                        ),
                                                                    ):
                                                                        await NexusServer.create(
                                                                            nats_url="nats://mock:4222"
                                                                        )

        # outcome_tracker.start() must NOT be called in create()
        mock_tracker.start.assert_not_called()


@pytest.mark.asyncio
async def test_start_calls_agent_manager_and_outcome_tracker():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    mock_nats.close = AsyncMock()
    mock_nats.subscribe_raw = AsyncMock()

    mock_tracker = MagicMock()
    mock_tracker.start = AsyncMock()
    mock_tracker.stop = AsyncMock()
    mock_tracker._poll_task = None

    mock_prescaler = MagicMock()
    mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

    mock_agent_manager = MagicMock()
    mock_agent_manager.start = AsyncMock()
    mock_agent_manager._tasks = []

    mock_notifier = MagicMock()
    mock_notifier.start_background = MagicMock()
    mock_notifier.stop = MagicMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.start = AsyncMock()
    mock_orchestrator.stop = AsyncMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        with patch("nexus.server.PpaOutcomeTracker", return_value=mock_tracker):
            with patch("nexus.server.Prescaler", return_value=mock_prescaler):
                with patch(
                    "nexus.server.AgentManager", return_value=mock_agent_manager
                ):
                    with patch("nexus.server.Notifier", return_value=mock_notifier):
                        with patch(
                            "nexus.server.NexusOrchestrator",
                            return_value=mock_orchestrator,
                        ):
                            with patch(
                                "nexus.server.RCAEngine", return_value=MagicMock()
                            ):
                                with patch(
                                    "nexus.server.AuditTrail",
                                    return_value=_awaitable_magicmock(),
                                ):
                                    with patch(
                                        "nexus.server.RollbackRegistry",
                                        return_value=MagicMock(),
                                    ):
                                        with patch(
                                            "nexus.server.PolicyEngine",
                                            return_value=_awaitable_magicmock(),
                                        ):
                                            with patch(
                                                "nexus.server.CooldownStore",
                                                return_value=_awaitable_magicmock(),
                                            ):
                                                with patch(
                                                    "nexus.server.GovernanceCircuitBreaker",
                                                    return_value=_awaitable_magicmock(),
                                                ):
                                                    with patch(
                                                        "nexus.server.HumanApprovalQueue",
                                                        return_value=MagicMock(),
                                                    ):
                                                        with patch(
                                                            "nexus.server.RunbookExecutor",
                                                            return_value=MagicMock(),
                                                        ):
                                                            with patch(
                                                                "nexus.server.OutcomeStore",
                                                                return_value=_awaitable_magicmock(),
                                                            ):
                                                                with patch(
                                                                    "nexus.server.KnowledgeBase",
                                                                    return_value=_awaitable_magicmock(),
                                                                ):
                                                                    with patch(
                                                                        "nexus.server.build_feedback_loop",
                                                                        new=AsyncMock(
                                                                            return_value=_awaitable_magicmock()
                                                                        ),
                                                                    ):
                                                                        server = await NexusServer.create(
                                                                            nats_url="nats://mock:4222"
                                                                        )

    with patch("nexus.server.uvicorn") as mock_uvicorn:
        mock_uvicorn.Server.return_value.serve = AsyncMock()
        mock_uvicorn.Config.return_value = MagicMock()
        await server.start()

    mock_agent_manager.start.assert_called_once()
    mock_tracker.start.assert_called_once()


@pytest.mark.asyncio
async def test_stop_cleans_up_all_subsystems():
    mock_nats = MagicMock()
    mock_nats.close = AsyncMock()
    mock_nats.connect = AsyncMock()
    mock_nats.subscribe_raw = AsyncMock()

    mock_tracker = MagicMock()
    mock_tracker.stop = AsyncMock()
    mock_tracker._poll_task = None

    mock_prescaler = MagicMock()
    mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

    mock_agent_manager = MagicMock()
    mock_agent_manager.stop = MagicMock()
    mock_agent_manager._tasks = []

    mock_notifier = MagicMock()
    mock_notifier.start_background = MagicMock()
    mock_notifier.stop = MagicMock()

    mock_orchestrator = MagicMock()
    mock_orchestrator.stop = AsyncMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        with patch("nexus.server.PpaOutcomeTracker", return_value=mock_tracker):
            with patch("nexus.server.Prescaler", return_value=mock_prescaler):
                with patch(
                    "nexus.server.AgentManager", return_value=mock_agent_manager
                ):
                    with patch("nexus.server.Notifier", return_value=mock_notifier):
                        with patch(
                            "nexus.server.NexusOrchestrator",
                            return_value=mock_orchestrator,
                        ):
                            with patch(
                                "nexus.server.RCAEngine", return_value=MagicMock()
                            ):
                                with patch(
                                    "nexus.server.AuditTrail",
                                    return_value=_awaitable_magicmock(),
                                ):
                                    with patch(
                                        "nexus.server.RollbackRegistry",
                                        return_value=MagicMock(),
                                    ):
                                        with patch(
                                            "nexus.server.PolicyEngine",
                                            return_value=_awaitable_magicmock(),
                                        ):
                                            with patch(
                                                "nexus.server.CooldownStore",
                                                return_value=_awaitable_magicmock(),
                                            ):
                                                with patch(
                                                    "nexus.server.GovernanceCircuitBreaker",
                                                    return_value=_awaitable_magicmock(),
                                                ):
                                                    with patch(
                                                        "nexus.server.HumanApprovalQueue",
                                                        return_value=MagicMock(),
                                                    ):
                                                        with patch(
                                                            "nexus.server.RunbookExecutor",
                                                            return_value=MagicMock(),
                                                        ):
                                                            with patch(
                                                                "nexus.server.OutcomeStore",
                                                                return_value=_awaitable_magicmock(),
                                                            ):
                                                                with patch(
                                                                    "nexus.server.KnowledgeBase",
                                                                    return_value=_awaitable_magicmock(),
                                                                ):
                                                                    with patch(
                                                                        "nexus.server.build_feedback_loop",
                                                                        new=AsyncMock(
                                                                            return_value=_awaitable_magicmock()
                                                                        ),
                                                                    ):
                                                                        server = await NexusServer.create(
                                                                            nats_url="nats://mock:4222"
                                                                        )

    server._tasks = [MagicMock() for _ in range(3)]
    with patch("nexus.server.asyncio.gather", AsyncMock(return_value=[])):
        await server.stop()

    mock_notifier.stop.assert_called_once()
    mock_agent_manager.stop.assert_called()
    mock_tracker.stop.assert_called_once()
    mock_nats.close.assert_called_once()
