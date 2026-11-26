"""
NEXUS Operator Authentication
==============================
The dashboard and the governance endpoints form the operator surface: they
expose incident details and policy overrides, and — most importantly — the
approve/reject endpoints that execute remediation on a live cluster.

``TokenStore`` is the SDK *application* token registry, so it is the wrong
principal for a human operator, and it also needs Postgres, which is optional
at runtime. Operators therefore authenticate with a dedicated bearer token held
in the server environment.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

__all__ = [
    "ADMIN_TOKEN_ENV",
    "DISABLE_AUTH_ENV",
    "operator_auth_enabled",
    "require_operator",
]

#: Environment variable holding the operator bearer token.
ADMIN_TOKEN_ENV = "NEXUS_ADMIN_TOKEN"

#: Environment variable that disables operator authentication entirely.
#: Intended for local development only; every request is logged when it is on.
DISABLE_AUTH_ENV = "NEXUS_DISABLE_OPERATOR_AUTH"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _configured_token() -> str:
    return (os.getenv(ADMIN_TOKEN_ENV) or "").strip()


def _auth_disabled() -> bool:
    return (os.getenv(DISABLE_AUTH_ENV) or "").strip().lower() in _TRUE_VALUES


def operator_auth_enabled() -> bool:
    """True when operator requests can be authenticated."""
    return bool(_configured_token())


async def require_operator(authorization: str | None = Header(None)) -> str:
    """Authenticate an operator request and return the operator identity.

    Fails closed: when no token is configured the protected routes are refused
    with a 503 that names the missing environment variable, rather than being
    silently opened.
    """
    if _auth_disabled():
        logger.warning(
            "[OperatorAuth] Authentication DISABLED via %s — operator requests are unauthenticated.",
            DISABLE_AUTH_ENV,
        )
        return "anonymous"

    expected = _configured_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "Operator authentication is not configured. "
                f"Set {ADMIN_TOKEN_ENV} on the NEXUS server, "
                f"or set {DISABLE_AUTH_ENV}=true for local development."
            ),
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization: Bearer <token>",
        )

    token = authorization[len("Bearer ") :].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid operator token")

    return "operator"
