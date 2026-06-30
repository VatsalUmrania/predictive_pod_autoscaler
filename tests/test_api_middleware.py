"""Tests for NEXUS API middleware."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient
from ppa.api.middleware import add_nexus_middleware


@pytest.fixture
def app():
    """Create a test FastAPI app."""
    app = FastAPI()

    @app.get("/ok")
    async def ok_endpoint():
        return {"status": "ok"}

    @app.get("/error")
    async def error_endpoint():
        raise HTTPException(status_code=500, detail="Internal error")

    @app.get("/slow")
    async def slow_endpoint():
        import asyncio

        await asyncio.sleep(0.6)  # Exceeds 500ms threshold
        return {"status": "ok"}

    return app


@pytest.mark.asyncio
async def test_middleware_added_with_token(app):
    """Test that middleware is added when token is set."""
    os.environ["SELFHEAL_TOKEN"] = "test-token"
    try:
        wrapped = add_nexus_middleware(app)
        assert wrapped is not app
        assert wrapped.__class__.__name__ == "SelfHealMiddleware"
    finally:
        os.environ.pop("SELFHEAL_TOKEN", None)


@pytest.mark.asyncio
async def test_middleware_skipped_no_token(app):
    """Test that middleware is skipped when no token."""
    os.environ.pop("SELFHEAL_TOKEN", None)
    wrapped = add_nexus_middleware(app)
    assert wrapped is app


@pytest.mark.asyncio
async def test_middleware_captures_500_error(app):
    """Test that middleware captures HTTP 500 errors."""
    os.environ["SELFHEAL_TOKEN"] = "test-token"
    os.environ["NEXUS_API_URL"] = "http://test:8081"

    try:
        wrapped = add_nexus_middleware(app)

        # Mock the NEXUS client to verify event was sent
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_post = AsyncMock()
        mock_post.return_value = mock_response
        mock_client.post = mock_post

        # Patch the middleware's internal client
        with patch(
            "nexus.sdk.python.middleware.httpx.AsyncClient", return_value=mock_client
        ):
            async with AsyncClient(app=wrapped, base_url="http://test") as client:
                response = await client.get("/error")
                assert response.status_code == 500

            # Verify NEXUS event was sent
            mock_post.assert_called_once()
    finally:
        os.environ.pop("SELFHEAL_TOKEN", None)
        os.environ.pop("NEXUS_API_URL", None)


@pytest.mark.asyncio
async def test_middleware_captures_slow_response(app):
    """Test that middleware captures slow responses (>500ms)."""
    os.environ["SELFHEAL_TOKEN"] = "test-token"
    os.environ["NEXUS_API_URL"] = "http://test:8081"

    try:
        wrapped = add_nexus_middleware(app)

        # Mock the NEXUS client
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_post = AsyncMock()
        mock_post.return_value = mock_response
        mock_client.post = mock_post

        with patch(
            "nexus.sdk.python.middleware.httpx.AsyncClient", return_value=mock_client
        ):
            async with AsyncClient(app=wrapped, base_url="http://test") as client:
                response = await client.get("/slow")
                assert response.status_code == 200

            # Verify NEXUS event was sent for slow response
            mock_post.assert_called_once()
    finally:
        os.environ.pop("SELFHEAL_TOKEN", None)
        os.environ.pop("NEXUS_API_URL", None)
