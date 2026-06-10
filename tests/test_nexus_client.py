"""Tests for NEXUS client integration."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from ppa.operator.nexus_client import (
    CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    CIRCUIT_BREAKER_THRESHOLD,
    NexusClient,
    get_nexus_client,
    reset_nexus_client,
)


@pytest.fixture
def reset_client():
    """Reset global client before each test."""
    reset_nexus_client()
    yield
    reset_nexus_client()


@pytest.mark.asyncio
async def test_send_event_success(reset_client):
    """Test successful event sending."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client directly
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_event("test_event", {"key": "value"})

    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_send_event_disabled(reset_client):
    """Test that events are not sent when disabled."""
    client = NexusClient(api_url="http://test:8081", token="")
    client._enabled = False

    with patch("httpx.AsyncClient") as mock_client:
        await client.send_event("test_event", {"key": "value"})
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_send_event_timeout_with_retry(reset_client):
    """Test timeout handling with retries."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    from httpx import TimeoutException
    mock_post = AsyncMock(side_effect=TimeoutException("Timeout"))
    mock_client.post = mock_post
    client._persistent_client = mock_client

    # Should retry MAX_RETRIES times
    await client.send_event("test_event", {"key": "value"})
    assert mock_post.call_count == 2  # MAX_RETRIES


@pytest.mark.asyncio
async def test_send_event_connect_error_no_retry(reset_client):
    """Test that connection errors don't trigger retries."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    from httpx import ConnectError
    mock_post = AsyncMock(side_effect=ConnectError("Connection refused"))
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_event("test_event", {"key": "value"})
    assert mock_post.call_count == 1  # No retry for connect errors


@pytest.mark.asyncio
async def test_circuit_breaker_opens(reset_client):
    """Test circuit breaker opens after threshold failures."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    from httpx import ConnectError
    mock_post = AsyncMock(side_effect=ConnectError("Connection refused"))
    mock_client.post = mock_post
    client._persistent_client = mock_client

    # Trigger threshold failures
    for _ in range(CIRCUIT_BREAKER_THRESHOLD):
        await client.send_event("test_event", {"key": "value"})

    assert client._failure_count >= CIRCUIT_BREAKER_THRESHOLD
    assert client._is_circuit_open()

    # Next call should be skipped (circuit breaker)
    call_count_before = mock_post.call_count
    await client.send_event("test_event", {"key": "value"})
    assert mock_post.call_count == call_count_before  # No new call


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_cooldown(reset_client):
    """Test circuit breaker resets after cooldown period."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    from httpx import ConnectError
    mock_post = AsyncMock(side_effect=ConnectError("Connection refused"))
    mock_client.post = mock_post
    client._persistent_client = mock_client

    # Trigger failures
    for _ in range(CIRCUIT_BREAKER_THRESHOLD):
        await client.send_event("test_event", {"key": "value"})

    assert client._is_circuit_open()

    # Simulate cooldown expiration
    client._last_failure_time = time.monotonic() - CIRCUIT_BREAKER_COOLDOWN_SECONDS - 1

    # Circuit should be closed now
    assert not client._is_circuit_open()


@pytest.mark.asyncio
async def test_event_deduplication(reset_client):
    """Test that identical events are deduplicated."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True
    client._event_cooldown_seconds = 10  # Short cooldown for testing

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    # Send first event
    await client.send_event("test_event", {"deployment": "test", "namespace": "default"})
    assert mock_post.call_count == 1

    # Send identical event immediately (should be deduplicated)
    await client.send_event("test_event", {"deployment": "test", "namespace": "default"})
    assert mock_post.call_count == 1  # No new call

    # Simulate cooldown by backdating cache entry
    cache_key = "test_event:default:test"
    if cache_key in client._last_event_cache:
        client._last_event_cache[cache_key] = time.monotonic() - client._event_cooldown_seconds - 1

    # Send again (should go through)
    await client.send_event("test_event", {"deployment": "test", "namespace": "default"})
    assert mock_post.call_count == 2  # New call


@pytest.mark.asyncio
async def test_cache_max_size(reset_client):
    """Test that cache respects max size limit."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True
    client._max_cache_size = 5  # Small limit for testing

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    # Fill cache beyond max size
    for i in range(10):
        await client.send_event("test_event", {"deployment": f"test{i}", "namespace": "default"})

    # Cache should be limited to max_size
    assert len(client._last_event_cache) <= client._max_cache_size


@pytest.mark.asyncio
async def test_send_scaling_decision(reset_client):
    """Test send_scaling_decision wrapper."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_scaling_decision(
        deployment="test-dep",
        namespace="test-ns",
        current=5,
        desired=10,
        predicted_load=500.0,
        horizon="rps_t10m",
    )

    # Verify payload structure
    call_args = mock_post.call_args
    assert call_args is not None
    json_data = call_args.kwargs.get("json", {})
    assert json_data.get("type") == "ppa_scaling_decision"
    assert json_data.get("deployment") == "test-dep"
    assert json_data.get("namespace") == "test-ns"
    assert json_data.get("current_replicas") == 5
    assert json_data.get("desired_replicas") == 10
    assert json_data.get("predicted_load") == 500.0
    assert json_data.get("horizon") == "rps_t10m"


@pytest.mark.asyncio
async def test_send_concept_drift(reset_client):
    """Test send_concept_drift wrapper."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_concept_drift(
        deployment="test-dep",
        namespace="test-ns",
        error_pct=25.5,
        severity="high",
    )

    call_args = mock_post.call_args
    json_data = call_args.kwargs.get("json", {})
    assert json_data.get("type") == "ppa_concept_drift"
    assert json_data.get("deployment") == "test-dep"
    assert json_data.get("namespace") == "test-ns"
    assert json_data.get("error_pct") == 25.5
    assert json_data.get("severity") == "high"


@pytest.mark.asyncio
async def test_send_prediction_error(reset_client):
    """Test send_prediction_error wrapper."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_prediction_error(
        deployment="test-dep",
        namespace="test-ns",
        error_msg="Model load failed",
    )

    call_args = mock_post.call_args
    json_data = call_args.kwargs.get("json", {})
    assert json_data.get("type") == "ppa_prediction_error"
    assert json_data.get("deployment") == "test-dep"
    assert json_data.get("namespace") == "test-ns"
    assert json_data.get("error_msg") == "Model load failed"


@pytest.mark.asyncio
async def test_send_circuit_breaker_trip(reset_client):
    """Test send_circuit_breaker_trip wrapper."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    client._persistent_client = mock_client

    await client.send_circuit_breaker_trip(
        deployment="test-dep",
        namespace="test-ns",
        failure_count=10,
    )

    call_args = mock_post.call_args
    json_data = call_args.kwargs.get("json", {})
    assert json_data.get("type") == "ppa_circuit_breaker_trip"
    assert json_data.get("deployment") == "test-dep"
    assert json_data.get("namespace") == "test-ns"
    assert json_data.get("failure_count") == 10


def test_get_singleton(reset_client):
    """Test singleton pattern."""
    client1 = get_nexus_client()
    client2 = get_nexus_client()
    assert client1 is client2


@pytest.mark.asyncio
async def test_persistent_client_reuse(reset_client):
    """Test that persistent client is reused across calls."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    mock_client.aclose = AsyncMock()
    client._persistent_client = mock_client

    # First call creates client
    await client.send_event("test1", {"key": "value"})
    first_client = client._persistent_client

    # Second call reuses client
    await client.send_event("test2", {"key": "value"})
    assert client._persistent_client is first_client


@pytest.mark.asyncio
async def test_close_persistent_client(reset_client):
    """Test that persistent client is closed properly."""
    client = NexusClient(api_url="http://test:8081", token="test-token")
    client._enabled = True

    # Mock the persistent client
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_post = AsyncMock()
    mock_post.return_value = mock_response
    mock_client.post = mock_post
    mock_client.aclose = AsyncMock()
    client._persistent_client = mock_client

    await client.send_event("test", {"key": "value"})
    await client.close()

    # Verify aclose was called
    mock_client.aclose.assert_called_once()
