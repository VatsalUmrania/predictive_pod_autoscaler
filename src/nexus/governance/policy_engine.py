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
        action_type: str = "",
        healing_level: int = 1,
        blast_radius: str = "unknown",
        in_cooldown: bool = False,
        governance_cb_open: bool = False,
        confidence: float = 1.0,
        human_approved: bool = False,
        override_blast_radius: bool = False,
        # Zero-Trust metadata parameters
        tool_name: str | None = None,
        parameters: dict[str, Any] | None = None,
        is_destructive: bool = False,
        allows_unbacked_rollback: bool = False,
        has_rollback: bool = True,
        environment: str = "production",
        target_service: str = "unknown",
    ) -> PolicyDecision:
        effective_tool = tool_name or action_type
        input_doc = {
            "input": {
                "action": {
                    "tool_name": effective_tool,
                    "type": action_type or effective_tool,
                    "level": healing_level,
                    "parameters": parameters or {},
                    "blast_radius": blast_radius,
                    "is_destructive": is_destructive,
                    "allows_unbacked_rollback": allows_unbacked_rollback,
                    "has_rollback": has_rollback,
                    "in_cooldown": in_cooldown,
                    "governance_cb_open": governance_cb_open,
                    "confidence": confidence,
                    "human_approved": human_approved,
                    "override_blast_radius": override_blast_radius,
                },
                "context": {
                    "environment": environment,
                    "target_service": target_service,
                    "cooldown_active": in_cooldown,
                    "circuit_breaker_open": governance_cb_open,
                    "operator_confirmed": human_approved,
                },
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
            # 1. Attempt unified composite governance endpoint (/v1/data/nexus/governance)
            try:
                gov_resp = await client.post(
                    f"{self._opa_url}/v1/data/nexus/governance", json=input_doc
                )
                if gov_resp.status_code == 200:
                    res = gov_resp.json().get("result")
                    if isinstance(res, dict) and ("allow" in res or "allowed" in res):
                        allowed = bool(res.get("allow", res.get("allowed", False)))
                        requires_approval = bool(
                            res.get(
                                "require_human_approval",
                                res.get("requires_approval", False),
                            )
                        )
                        deny_reasons = list(res.get("deny_reasons", []))
                        return PolicyDecision(
                            allowed=allowed,
                            requires_approval=requires_approval,
                            deny_reasons=deny_reasons,
                            source="opa",
                        )
                elif gov_resp.status_code not in (404, 400):
                    gov_resp.raise_for_status()
            except (httpx.RequestError, httpx.HTTPStatusError):
                raise
            except Exception as exc:
                logger.debug(
                    "[PolicyEngine] Batch /governance query failed or unsupported (%s), falling back to legacy endpoints",
                    exc,
                )

            # 2. Legacy individual endpoints (/allow_action, /deny_reasons, /requires_human_approval)
            allow_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/allow_action", json=input_doc
            )
            allow_resp.raise_for_status()
            allowed = bool(allow_resp.json().get("result", False))

            deny_reasons: list[str] = []
            deny_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/deny_reasons", json=input_doc
            )
            if deny_resp.status_code == 200:
                raw = deny_resp.json().get("result", [])
                deny_reasons = list(raw) if raw else []

            requires_approval = False
            appr_resp = await client.post(
                f"{self._opa_url}/v1/data/nexus/requires_human_approval", json=input_doc
            )
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
