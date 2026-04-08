"""Prometheus metric queries with circuit breaker resilience.

This module handles all Prometheus interactions, including:
- Query execution with exponential backoff
- Parallel query execution for performance
- Circuit breaker pattern to prevent cascading failures
- Thread-local URL storage for multi-region support (PR#18)
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

import requests

from ppa.config import PROM_FAILURE_THRESHOLD, PROMETHEUS_URL, FeatureVectorError

logger = logging.getLogger("ppa.infrastructure.prometheus")

# FIX (PR#18): Per-query Prometheus URL storage for multi-region support
# Uses threading.local() to ensure each thread has its own URL without cross-thread contamination
_thread_local_storage = threading.local()

# Module-level counter: consecutive Prometheus failures across all queries
# NOTE: This is deprecated. Use CRState.prom_failures and CRState.prom_last_failure_time instead.
# Keeping for backward compatibility with standalone tools.
_prom_consecutive_failures: int = 0
_prom_last_failure_time: float = 0.0


class PrometheusCircuitBreakerError(FeatureVectorError):
    """Raised when Prometheus circuit breaker is active due to repeated failures."""

    pass


# Backward compatibility alias
PrometheusCircuitBreakerTripped = PrometheusCircuitBreakerError


def set_prometheus_url(url: str) -> None:
    """Set the Prometheus URL for the current thread's execution context (thread-safe via threading.local).

    This enables multi-region support where different threads (e.g., in prom_query_parallel)
    can query different Prometheus instances without interfering with each other.

    Args:
        url: Prometheus server URL (e.g., "http://prometheus:9090")

    Example:
        >>> set_prometheus_url("http://prometheus-us-west:9090")
        >>> result = prom_query("up")  # Uses the set URL in this thread
    """
    _thread_local_storage.prom_url = url


def get_current_prometheus_url() -> str:
    """Get the Prometheus URL for the current thread.

    Returns the thread-local URL if set, otherwise defaults to PROMETHEUS_URL.
    Thread-local isolation prevents one thread's URL from affecting another.

    Returns:
        Prometheus URL for the current thread context

    Example:
        >>> url = get_current_prometheus_url()
        >>> print(url)  # "http://prometheus:9090"
    """
    return getattr(_thread_local_storage, 'prom_url', PROMETHEUS_URL)


def _get_circuit_breaker(cr_state: object | None) -> tuple[int, float]:
    """Retrieve circuit breaker state from CRD status.

    Circuit breaker prevents cascading failures when Prometheus is unavailable.
    After `PROM_FAILURE_THRESHOLD` consecutive failures (default 10), the circuit
    opens for 60 seconds—metric queries are skipped entirely to prevent thundering
    herd of timeout errors.

    State persists in the CRD status, enabling multi-pod operator deployments to
    share resilience state without external storage (Kopf manages persistence).

    Args:
        cr_state: Current CRD status object containing circuit breaker data.
                  Falls back to module-level state if cr_state is unavailable.
                  Format (if present): {'prom_failures': int, 'prom_last_failure_time': float}

    Returns:
        Tuple of (failure_count, last_failure_timestamp_epoch).
        If state is empty/invalid, returns (0, 0.0) → circuit closed, queries allowed.

    Example:
        >>> failures, last_time = _get_circuit_breaker(crd_status)
        >>> if failures > 10:
        ...     print("Circuit breaker OPEN for 60s - skipping Prometheus")
        >>> else:
        ...     print(f"Operational: {failures}/10 failures")

    Design Notes:
        - Thread-safe: state read from CRD, not shared memory
        - Fallback: uses module-level state for backward compatibility
        - Recovery: circuit auto-opens after 60s (checked on next reconciliation)

    See Also:
        _set_circuit_breaker: Update circuit breaker state
        PrometheusCircuitBreakerError: Raised when circuit is open
        PR#11: Circuit breaker design doc
    """
    # Use duck typing: if cr_state has the required attributes, use it
    # This supports both CRState and test mocks with the same attributes
    if cr_state is not None and hasattr(cr_state, 'prom_failures') and hasattr(cr_state, 'prom_last_failure_time'):
        return cr_state.prom_failures, cr_state.prom_last_failure_time  # type: ignore[union-attr]
    return _prom_consecutive_failures, _prom_last_failure_time


def _set_circuit_breaker(cr_state: object | None, failures: int, last_time: float) -> None:
    """Update circuit breaker state in CRD status.

    Persists the current failure count and timestamp to the CRD status object,
    enabling state to survive operator restarts and pod evictions.

    Args:
        cr_state: CRD status object to update (None → use module-level fallback).
                  Will set attributes: prom_failures, prom_last_failure_time
        failures: Current failure count (0-N, where N >= threshold means circuit open)
        last_time: Epoch timestamp of last failure (used to calculate recovery time)

    Example:
        >>> # Record another Prometheus failure
        >>> _set_circuit_breaker(crd_status, failures=5, last_time=time.time())
        >>>
        >>> # Reset on success
        >>> _set_circuit_breaker(crd_status, failures=0, last_time=0.0)

    Design Notes:
        - Idempotent: safe to call multiple times with same values
        - Stateless: state stored in CRD, not operator memory
        - Backward compatible: falls back to module-level state if cr_state unavailable

    See Also:
        _get_circuit_breaker: Retrieve circuit breaker state
    """
    global _prom_consecutive_failures, _prom_last_failure_time
    
    # Use duck typing: if cr_state has the required attributes, use it
    # This supports both CRState and test mocks with the same attributes
    if cr_state is not None and hasattr(cr_state, 'prom_failures') and hasattr(cr_state, 'prom_last_failure_time'):
        cr_state.prom_failures = failures  # type: ignore[union-attr]
        cr_state.prom_last_failure_time = last_time  # type: ignore[union-attr]
    else:
        # Fallback to module-level state for backward compatibility
        _prom_consecutive_failures = failures
        _prom_last_failure_time = last_time


def prom_query(
    query: str, prom_url: str | None = None, cr_state: object | None = None
) -> float | None:
    """Query Prometheus with exponential backoff on failures. Circuit breaks after PROM_FAILURE_THRESHOLD failures.

    Implements resilient metric querying with:
    - Fast timeout (2s) to fail-fast on network issues
    - Circuit breaker to prevent thundering herd
    - Exponential backoff (2^n seconds, capped at 5 minutes)
    - Per-CR state for multi-instance operators

    Args:
        query: PromQL query string
        prom_url: Optional custom Prometheus URL (PR#18: multi-region support)
        cr_state: Optional CRState for per-CR circuit breaker (PR#1 fix)

    Returns:
        Query result (float) or None if failed

    Raises:
        PrometheusCircuitBreakerTripped: If circuit is open (backing off from failures)
        PrometheusCircuitBreakerError: If circuit breaker permanently active

    Example:
        >>> result = prom_query("rate(requests_total[1m])")
        >>> if result is not None:
        ...     print(f"Current RPS: {result}")
        >>> else:
        ...     print("Query failed")

    See Also:
        prom_query_parallel: Execute multiple queries concurrently
        _get_circuit_breaker: Check current circuit breaker state
    """
    # FIX (PR#1): Get circuit breaker state (per-CR or fallback to module-level)
    prom_failures, prom_last_failure_time = _get_circuit_breaker(cr_state)

    # FIX (PR#18): Use provided URL or fall back to current context URL
    url = prom_url or get_current_prometheus_url()

    # FIX (PR#9): Check circuit breaker status
    if prom_failures >= PROM_FAILURE_THRESHOLD:
        # Circuit breaker active: apply exponential backoff
        backoff_time = min(300, 2 ** min(prom_failures - PROM_FAILURE_THRESHOLD, 10))
        elapsed = time.time() - prom_last_failure_time
        if elapsed < backoff_time:
            # Still in backoff, raise exception to block feature extraction
            raise PrometheusCircuitBreakerTripped(
                f"Prometheus circuit breaker active for {elapsed:.0f}s (backoff: {backoff_time}s)"
            )

    try:
        resp = requests.get(
            f"{url}/api/v1/query",
            params={"query": query},
            timeout=2,  # Short timeout to fail fast
        )
        resp.raise_for_status()
        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return None
        _set_circuit_breaker(cr_state, 0, 0.0)  # Reset on success
        return float(result[0]["value"][1])
    except Exception as exc:
        prom_failures += 1
        prom_last_failure_time = time.time()
        _set_circuit_breaker(cr_state, prom_failures, prom_last_failure_time)

        if prom_failures >= PROM_FAILURE_THRESHOLD:
            logger.critical(
                f"Prometheus circuit breaker TRIPPED ({prom_failures}/{PROM_FAILURE_THRESHOLD} failures): {exc}"
            )
            raise PrometheusCircuitBreakerError(str(exc)) from exc
        else:
            logger.warning(
                f"Prometheus query failed ({prom_failures}/{PROM_FAILURE_THRESHOLD}): {exc}"
            )
        return None


def prom_query_parallel(
    queries: dict[str, str],
    max_workers: int = 5,
    timeout: float = 2.0,
    prom_url: str | None = None,
    cr_state: object | None = None,
) -> dict[str, float | None]:
    """Execute Prometheus queries in parallel using ThreadPoolExecutor.

    Optimizes metric collection by querying multiple metrics concurrently.
    Uses per-thread URL storage (PR#18) to enable multi-region support where
    different threads query different Prometheus instances.

    FIX (PR#20): Execute Prometheus queries in parallel using ThreadPoolExecutor.

    Args:
        queries: Dict mapping feature_name -> promql query string
        max_workers: Number of parallel threads (default 5)
        timeout: Per-query timeout in seconds
        prom_url: Optional custom Prometheus URL (PR#18: multi-region support)
        cr_state: Optional CRState for per-CR circuit breaker (PR#1 fix)

    Returns:
        Dict mapping feature_name -> result (float or None if failed)

    Raises:
        PrometheusCircuitBreakerError: If circuit breaker is open

    Example:
        >>> queries = {
        ...     'rps': 'rate(requests_total[1m])',
        ...     'cpu': 'avg(cpu_usage_percent)',
        ...     'memory': 'avg(memory_mb)'
        ... }
        >>> results = prom_query_parallel(queries, max_workers=3)
        >>> print(results)  # {'rps': 100.5, 'cpu': 45.2, 'memory': 512.1}

    Design Notes:
        - Thread-safe: uses threading.local() for per-thread URL storage
        - Resilient: individual query failures don't block others
        - Circuit-aware: propagates circuit breaker failures to abort all queries
        - Timeout handling: per-query timeouts prevent hanging threads

    See Also:
        prom_query: Execute single query with circuit breaker
        _query_single: Helper for individual query execution
    """
    results: dict[str, float | None] = {}

    def _query_single(feature_query_pair: tuple[str, str]) -> tuple[str, float | None]:
        feature_name, query = feature_query_pair
        try:
            result = prom_query(query, prom_url=prom_url, cr_state=cr_state)  # PR#18: pass custom URL; PR#1: pass CR state
            return feature_name, result
        except PrometheusCircuitBreakerError:
            raise  # Re-raise circuit breaker to stop all queries
        except Exception as exc:
            logger.debug(f"Query failed for {feature_name}: {exc}")
            return feature_name, None

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all queries
            future_to_feature = {
                executor.submit(_query_single, (name, query)): name
                for name, query in queries.items()
            }

            # Collect results with timeout
            for future in future_to_feature:
                feature_name = future_to_feature[future]
                try:
                    _, result = future.result(timeout=timeout)
                    results[feature_name] = result
                except FutureTimeoutError:
                    logger.warning(f"Query timeout for {feature_name}")
                    results[feature_name] = None
                except PrometheusCircuitBreakerError:
                    raise  # Propagate circuit breaker
                except Exception as exc:
                    logger.debug(f"Query exception for {feature_name}: {exc}")
                    results[feature_name] = None

    except PrometheusCircuitBreakerError:
        # Circuit breaker active - all queries should fail
        logger.error("Circuit breaker active, aborting parallel queries")
        raise

    return results


__all__ = [
    "PrometheusCircuitBreakerError",
    "PrometheusCircuitBreakerTripped",
    "prom_query",
    "prom_query_parallel",
    "set_prometheus_url",
    "get_current_prometheus_url",
]
