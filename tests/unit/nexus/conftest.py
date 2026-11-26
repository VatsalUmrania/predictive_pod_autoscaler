"""
Shared fixtures for the NEXUS unit tests.
"""

from __future__ import annotations

import pytest

from nexus.services.api.status.status_api import app
from nexus.services.gateway.auth.operator_auth import require_operator


@pytest.fixture(autouse=True)
def _operator_auth_override():
    """Let the operator routes answer without a bearer token.

    The live routes require ``NEXUS_ADMIN_TOKEN``. These tests exercise route
    behaviour, not the credential check, so the dependency is overridden for
    the whole directory. Tests that assert on the authentication itself clear
    this override.
    """
    app.dependency_overrides[require_operator] = lambda: "test-operator"
    yield
    app.dependency_overrides.pop(require_operator, None)
