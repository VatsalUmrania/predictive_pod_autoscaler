"""NEXUS Policy Engine.

Evaluates healing actions against OPA (Open Policy Agent).
"""
from __future__ import annotations

import logging
import time

import httpx

from nexus.governance.policy_models import PolicyDecision

logger = logging.getLogger(__name__)


class PolicyEngine:
    """Evaluates healing actions against OPA."""

    def __init__(
        self,
        opa_url: str = "http://localhost:8181",
        http_timeout: float = 2.0,
    ):
        self._opa_url = opa_url.rstrip("/")
        self._http_timeout = http_timeout
        self._opa_available = True
        self._opa_retry_after: float = 0.0
        self._opa_retry_cooldown_s = 60.0

    async def is_healthy(self) -> bool:
        """Check if OPA is reachable."""
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(f"{self._opa_url}/health")
                self._opa_available = resp.status_code == 200
                return self._opa_available
        except Exception:
            self._opa_available = False
            return False

    async def evaluate(
        self,
        action_type: str,
        healing_level: int,
        blast_radius: str = "unknown",
        in_cooldown: bool = False,
        governance_cb_open: bool = False,
        confidence: float = 1.0,
        human_approved: bool = False,
        override_blast_radius: bool = False,
    ) -> PolicyDecision:
        input_doc = {
            "input": {
                "action": {
                    "type": action_type,
                    "level": healing_level,
                    "blast_radius": blast_radius,
                    "in_cooldown": in_cooldown,
                    "governance_cb_open": governance_cb_open,
                    "confidence": confidence,
                    "human_approved": human_approved,
                    "override_blast_radius": override_blast_radius,
                }
            }
        }

        if not self._opa_available and time.monotonic() >= self._opa_retry_after:
            self._opa_available = True

        if self._opa_available:
            try:
                return await self._query_opa(input_doc)
            except Exception as exc:
                self._opa_available = False
                self._opa_retry_after = time.monotonic() + self._opa_retry_cooldown_s
                logger.warning(
                    f"[PolicyEngine] OPA error ({exc}) — denying (fail-closed), "
                    f"will retry in {self._opa_retry_cooldown_s}s"
                )

        return PolicyDecision(
            allowed=False,
            deny_reasons=["opa_unavailable_deny_closed"],
            source="error",
        )

    async def _query_opa(self, input_doc: dict) -> PolicyDecision:
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            allow_resp = await client.post(f"{self._opa_url}/v1/data/nexus/allow_action", json=input_doc)
            allow_resp.raise_for_status()
            allowed = bool(allow_resp.json().get("result", False))

            deny_reasons: list[str] = []
            deny_resp = await client.post(f"{self._opa_url}/v1/data/nexus/deny_reasons", json=input_doc)
            if deny_resp.status_code == 200:
                raw = deny_resp.json().get("result", [])
                deny_reasons = list(raw) if raw else []

            requires_approval = False
            appr_resp = await client.post(f"{self._opa_url}/v1/data/nexus/requires_human_approval", json=input_doc)
            if appr_resp.status_code == 200:
                requires_approval = bool(appr_resp.json().get("result", False))

            return PolicyDecision(
                allowed=allowed,
                requires_approval=requires_approval,
                deny_reasons=deny_reasons,
                source="opa",
            )

    @property
    def opa_available(self) -> bool:
        return self._opa_available
