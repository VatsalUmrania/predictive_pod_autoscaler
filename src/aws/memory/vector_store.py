"""
Phase 9 — Memory: Vector Store (Semantic Search)
==================================================
Stores incident embeddings for semantic similarity search.
"Find incidents similar to this one" — even if the exact resource name differs.

Current implementation: DynamoDB-backed simple text search (no external dependency).
Future: Replace with AWS OpenSearch or Qdrant for true vector similarity.

To upgrade to Qdrant:
    pip install qdrant-client
    client = QdrantClient(host="localhost", port=6333)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def search_similar_incidents(
    query: str,
    resource_type: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Search for incidents similar to the current one.

    Current implementation: keyword-based matching on root_cause text.
    Future: embed query with a model and do cosine similarity search.

    Args:
        query:         Describe the current incident (e.g. "Lambda OOM error")
        resource_type: Filter by resource type ("lambda", "sqs", "dynamodb")
        limit:         Max results to return

    Returns:
        List of similar past incidents with root_cause and action_taken.
    """
    # Stub — returns empty list until vector DB is configured
    # When you add Qdrant: replace this with actual vector search
    logger.debug(f"[VectorStore] search_similar_incidents('{query}') — stub, returning empty")
    return []


def index_incident(
    incident_id: str,
    root_cause: str,
    action_taken: str,
    resolved: bool,
    resource_type: str = "",
) -> bool:
    """
    Index an incident for future similarity search.

    Current implementation: no-op stub.
    Future: embed root_cause with a sentence transformer and store in Qdrant.
    """
    logger.debug(f"[VectorStore] index_incident({incident_id}) — stub, skipping")
    return True


# ── Future: Qdrant integration template ──────────────────────────────────────
#
# from qdrant_client import QdrantClient
# from qdrant_client.models import Distance, VectorParams, PointStruct
# from sentence_transformers import SentenceTransformer
#
# _model = SentenceTransformer("all-MiniLM-L6-v2")
# _client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
# _COLLECTION = "nexus_incidents"
#
# def search_similar_incidents(query, limit=3):
#     vec = _model.encode(query).tolist()
#     results = _client.search(collection_name=_COLLECTION, query_vector=vec, limit=limit)
#     return [r.payload for r in results]
#
# def index_incident(incident_id, root_cause, ...):
#     vec = _model.encode(root_cause).tolist()
#     _client.upsert(_COLLECTION, points=[PointStruct(id=incident_id, vector=vec, payload={...})])
