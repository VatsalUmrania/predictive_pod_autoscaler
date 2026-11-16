"""Tests for AsyncLLMRateLimiter and RateLimitedChatModel."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from nexus.graph.llm_limiter import (
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
    mock_base.ainvoke.side_effect = Exception("429 Resource has been exhausted (e.g. check quota)")

    limiter = AsyncLLMRateLimiter(rpm=100, max_concurrent=1)
    proxy = RateLimitedChatModel(mock_base, limiter=limiter)

    with pytest.raises(LLMQuotaExhaustedError) as exc_info:
        await proxy.ainvoke("test")

    assert "quota exceeded" in str(exc_info.value).lower()
