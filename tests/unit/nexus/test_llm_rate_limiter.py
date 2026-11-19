"""Tests for AsyncLLMRateLimiter and RateLimitedChatModel."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.engine.workflow.llm_limiter import (
    AsyncLLMRateLimiter,
    LLMQuotaExhaustedError,
    RateLimitedChatModel,
)


@pytest.mark.asyncio
async def test_limiter_concurrency_lock():
    """Verify rate limiter allows only 1 concurrent call with semaphore(1)."""
    limiter = AsyncLLMRateLimiter(rpm=60, max_concurrent=1)
    concurrent_count = 0
    max_observed_concurrent = 0

    async def worker():
        nonlocal concurrent_count, max_observed_concurrent
        async with limiter:
            concurrent_count += 1
            max_observed_concurrent = max(max_observed_concurrent, concurrent_count)
            await asyncio.sleep(0.01)
            concurrent_count -= 1

    await asyncio.gather(worker(), worker(), worker())
    assert max_observed_concurrent == 1


@pytest.mark.asyncio
async def test_rate_limited_chat_model_delegation():
    """Verify RateLimitedChatModel delegates ainvoke to underlying model with limiter."""
    mock_base = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.content = "response content"
    mock_base.ainvoke.return_value = mock_resp

    limiter = AsyncLLMRateLimiter(rpm=100, max_concurrent=1)
    proxy = RateLimitedChatModel(mock_base, limiter=limiter)

    res = await proxy.ainvoke(["test prompt"])
    assert res.content == "response content"
    assert mock_base.ainvoke.call_count == 1


@pytest.mark.asyncio
async def test_rate_limited_chat_model_catches_429_quota():
    """Verify RateLimitedChatModel intercepts 429 RESOURCE_EXHAUSTED."""
    mock_base = AsyncMock()
    mock_base.ainvoke.side_effect = Exception(
        "429 Resource has been exhausted (e.g. check quota)"
    )

    limiter = AsyncLLMRateLimiter(rpm=100, max_concurrent=1)
    proxy = RateLimitedChatModel(mock_base, limiter=limiter)

    with pytest.raises(LLMQuotaExhaustedError) as exc_info:
        await proxy.ainvoke("test")

    assert "quota exceeded" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_langgraph_retry_policy_recovers_from_transient_504_and_escalates_on_exhaustion():
    """
    Verify LangGraph's native RetryPolicy retries transient 504/TimeoutError on diagnose_node
    and LangGraph's native error_handler gracefully routes to escalate when retries exhaust.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import RetryPolicy

    from nexus.engine.workflow.builder import (
        build_incident_graph,
        is_retryable_node_error,
    )

    # 1. Verify predicate retries transient timeouts/5xx but fast-fails on 429 quota
    assert is_retryable_node_error(TimeoutError("504 DEADLINE_EXCEEDED")) is True
    assert is_retryable_node_error(LLMQuotaExhaustedError("429 quota")) is False
    assert is_retryable_node_error(ValueError("bad schema")) is False

    # 2. Transient failure on attempt 1, recovers cleanly on attempt 2 via native RetryPolicy
    attempts = 0

    async def flaky_diagnose(state):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("504 DEADLINE_EXCEEDED")
        return {
            "platform": "k8s",
            "namespace": "shop-ns",
            "resource_kind": "Deployment",
            "service_name": "shop-demo",
            "target_resource": "shop-ns/Deployment/shop-demo",
            "failure_class": "readiness_probe_exit_zero",
            "confidence": 0.9,
            "rca_result": {"can_auto_remediate": False, "confidence": 0.9},
            "status": "DIAGNOSED",
        }

    fast_retry = RetryPolicy(
        max_attempts=3,
        initial_interval=0.01,
        jitter=False,
        retry_on=is_retryable_node_error,
    )
    graph = build_incident_graph(
        checkpointer=MemorySaver(),
        nodes={"diagnose": flaky_diagnose},
        retry_policy=fast_retry,
    )
    res = await graph.ainvoke(
        {"incident_id": "inc-retry-ok", "raw_events": [{"message": "Unhealthy"}]},
        config={"configurable": {"thread_id": "inc-retry-ok"}},
    )
    assert attempts == 2
    assert res["target_resource"] == "shop-ns/Deployment/shop-demo"

    # 3. Exhausted retries route gracefully to escalate via native error_handler (no crash)
    exhaust_attempts = 0

    async def always_timeout_diagnose(state):
        nonlocal exhaust_attempts
        exhaust_attempts += 1
        raise TimeoutError("504 DEADLINE_EXCEEDED")

    graph_exhaust = build_incident_graph(
        checkpointer=MemorySaver(),
        nodes={"diagnose": always_timeout_diagnose},
        retry_policy=fast_retry,
    )
    res_esc = await graph_exhaust.ainvoke(
        {"incident_id": "inc-retry-esc", "raw_events": [{"message": "Unhealthy"}]},
        config={"configurable": {"thread_id": "inc-retry-esc"}},
    )
    assert exhaust_attempts == 3
    assert res_esc["status"] == "ESCALATED"
    assert "504 DEADLINE_EXCEEDED" in (res_esc.get("error") or "")
