"""
Tests for status_api NexusContext lifecycle: _lifespan must skip self-init
when context.orchestrator (or any NexusContext field) is already populated.
"""

import asyncio
from unittest.mock import MagicMock, patch

from fastapi import FastAPI

from nexus.observability import status_api as status_api_module


def test_lifespan_skips_all_self_init_when_context_orchestrator_pre_populated():
    """
    When context.orchestrator is already set (by NexusServer.create()),
    _lifespan must NOT attempt any self-init: no TokenStore, no NATS,
    no PpaOutcomeTracker, no Prescaler.
    """
    test_app = FastAPI()

    # Pre-populate as NexusServer.create() would
    status_api_module.context.orchestrator = MagicMock()
    status_api_module.context.prescaler = MagicMock()
    status_api_module.context.ppa_outcome_tracker = MagicMock()
    status_api_module.context.audit_trail = MagicMock()
    status_api_module.context.feedback_loop = MagicMock()
    status_api_module.context.outcome_store = MagicMock()
    status_api_module.context.knowledge_base = MagicMock()
    status_api_module.context.runbook_library = MagicMock()

    nats_instantiated: list = []
    tracker_started: list = []
    tokenstore_inits: list = []

    def fake_nats_client(*args, **kwargs):
        nats_instantiated.append(True)
        client = MagicMock()
        client.connect = MagicMock()
        client.close = MagicMock()
        client.subscribe = MagicMock()
        return client

    def fake_tracker(*args, **kwargs):
        tracker = MagicMock()
        tracker.start = MagicMock()
        tracker.stop = MagicMock()
        return tracker

    fake_token_store = MagicMock()
    fake_token_store.init = MagicMock()

    try:
        with patch.dict(
            "sys.modules",
            {
                "nexus.bus.nats_client": MagicMock(NATSClient=fake_nats_client),
                "nexus.learning.ppa_outcome_tracker": MagicMock(
                    PpaOutcomeTracker=fake_tracker
                ),
                "nexus.predictive.prescaler": MagicMock(),
                "nexus.integration.token_store": MagicMock(
                    get_token_store=MagicMock(return_value=fake_token_store)
                ),
            },
        ):

            async def run_lifespan():
                async with status_api_module._lifespan(test_app):
                    pass

            asyncio.run(run_lifespan())

        # When context.orchestrator is populated, NONE of these should happen
        assert (
            len(nats_instantiated) == 0
        ), f"NATSClient was instantiated despite pre-populated context: {nats_instantiated!r}"
        assert (
            len(tracker_started) == 0
        ), f"PpaOutcomeTracker was started despite pre-populated context: {tracker_started!r}"
        assert (
            len(tokenstore_inits) == 0
        ), f"TokenStore was inited despite pre-populated context: {tokenstore_inits!r}"
    finally:
        # Tear down global context
        status_api_module.context.orchestrator = None
        status_api_module.context.prescaler = None
        status_api_module.context.ppa_outcome_tracker = None
        status_api_module.context.audit_trail = None
        status_api_module.context.feedback_loop = None
        status_api_module.context.outcome_store = None
        status_api_module.context.knowledge_base = None
        status_api_module.context.runbook_library = None
