"""
NEXUS LLM Concurrency and Rate Limiting
=======================================
Protects against API quota exhaustion (429 RESOURCE_EXHAUSTED) and prevents
event loop saturation from concurrent LLM requests. Transient 5xx/timeout
retries are delegated to LangGraph's native RetryPolicy in builder.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


class LLMQuotaExhaustedError(RuntimeError):
    """Raised when an LLM provider returns a 429 quota exhaustion error."""


class AsyncLLMRateLimiter:
    """Token bucket / interval rate limiter with concurrency semaphore."""

    def __init__(self, rpm: int = 15, max_concurrent: int = 1) -> None:
        self.rpm = max(rpm, 1)
        self.min_interval = 60.0 / self.rpm
        self.semaphore = asyncio.Semaphore(max(max_concurrent, 1))
        self._last_call_time = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        await self.semaphore.acquire()
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self.min_interval:
                sleep_s = self.min_interval - elapsed
                await asyncio.sleep(sleep_s)
            self._last_call_time = time.monotonic()

    def release(self) -> None:
        self.semaphore.release()

    async def __aenter__(self) -> AsyncLLMRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


_global_limiter: AsyncLLMRateLimiter | None = None


def get_global_rate_limiter() -> AsyncLLMRateLimiter:
    global _global_limiter
    if _global_limiter is None:
        rpm = int(os.getenv("NEXUS_LLM_RPM", "15"))
        concurrent = int(os.getenv("NEXUS_MAX_CONCURRENT_LLM", "1"))
        _global_limiter = AsyncLLMRateLimiter(rpm=rpm, max_concurrent=concurrent)
    return _global_limiter


def is_rate_limit_error(exc: Exception) -> bool:
    err_str = str(exc).lower()
    return (
        "429" in err_str
        or "resource_exhausted" in err_str
        or "resourceexhausted" in err_str
        or "quota" in err_str
        or "rate limit" in err_str
    )


class RateLimitedChatModel:
    """Transparent proxy that applies rate limiting and non-blocking 429 handling."""

    def __init__(
        self, delegate: Any, limiter: AsyncLLMRateLimiter | None = None
    ) -> None:
        self._delegate = delegate
        self._limiter = limiter or get_global_rate_limiter()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> Any:
        bound = self._delegate.bind_tools(tools, **kwargs)
        return RateLimitedChatModel(bound, self._limiter)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        structured = self._delegate.with_structured_output(schema, **kwargs)
        return RateLimitedChatModel(structured, self._limiter)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        await self._limiter.acquire()
        try:
            call = getattr(self._delegate, "ainvoke", None)
            if callable(call):
                return await call(*args, **kwargs)
            return await asyncio.to_thread(self._delegate.invoke, *args, **kwargs)
        except Exception as exc:
            if is_rate_limit_error(exc):
                logger.warning(
                    "[LLM RateLimiter] Quota exhausted (429): %s; failing fast",
                    exc,
                )
                raise LLMQuotaExhaustedError(f"LLM quota exceeded: {exc}") from exc
            raise
        finally:
            self._limiter.release()

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._delegate.invoke(*args, **kwargs)
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise LLMQuotaExhaustedError(f"LLM quota exceeded: {exc}") from exc
            raise
