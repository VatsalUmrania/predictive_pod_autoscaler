"""
Factory for instantiating FeedbackLoop with dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bus.nats_client import NATSClient
    from nexus.learning.feedback_loop import FeedbackLoop
    from nexus.learning.knowledge_base import KnowledgeBase
    from nexus.learning.outcome_store import OutcomeStore
    from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
    from nexus.reasoning.confidence_scorer import ConfidenceScorer


async def build_feedback_loop(
    confidence_scorer: ConfidenceScorer,
    nats_client: NATSClient | None = None,
    ppa_outcome_tracker: PpaOutcomeTracker | None = None,
    audit_db_path: str | None = None,
    knowledge_db_path: str | None = None,
    outcome_store: OutcomeStore | None = None,
    knowledge_base: KnowledgeBase | None = None,
    interval_s: float = 300.0,
) -> FeedbackLoop:
    """Build and initialize a FeedbackLoop with its dependencies."""
    from nexus.learning.feedback_loop import FeedbackLoop
    from nexus.learning.knowledge_base import KnowledgeBase
    from nexus.learning.outcome_store import OutcomeStore

    if outcome_store is None:
        store = OutcomeStore(db_path=audit_db_path)
        await store.connect()
    else:
        store = outcome_store

    if knowledge_base is None:
        kb = KnowledgeBase(db_path=knowledge_db_path)
        await kb.initialize()
    else:
        kb = knowledge_base

    return FeedbackLoop(
        outcome_store=store,
        knowledge_base=kb,
        confidence_scorer=confidence_scorer,
        nats_client=nats_client,
        ppa_outcome_tracker=ppa_outcome_tracker,
        interval_s=interval_s,
        verify_window_s=60.0,
    )
