"""Incident database operations repository."""

from __future__ import annotations

import json
from typing import Any

from nexus.db.pool import PostgresPool
from nexus.db.utils import _ensure_uuid


class IncidentRepository:
    """Handles CRUD queries for incidents and transitions."""

    def __init__(self, pool: PostgresPool):
        self._pool = pool

    async def create_incident(
        self,
        *,
        fingerprint: str,
        environment: str,
        target_resource: str,
        severity: str = "error",
        trigger_source: str = "unknown",
        trigger_payload: dict | None = None,
        incident_id: str | None = None,
    ) -> str:
        inc_uuid = _ensure_uuid(incident_id)
        inc_id = str(inc_uuid)
        payload_json = json.dumps(trigger_payload or {})
        sev = (severity or "error").lower()
        if sev == "emergency":
            sev = "critical"
        elif sev not in ("info", "warning", "error", "critical"):
            sev = "error"
        env = (environment or "kubernetes").lower()
        if env not in ("kubernetes", "aws", "hybrid", "on_prem"):
            env = "kubernetes"

        sql = """
        INSERT INTO incidents (
            incident_id, fingerprint, environment, target_resource,
            severity, current_state, trigger_source, trigger_payload
        ) VALUES ($1, $2, $3::incident_environment, $4, $5::incident_severity, 'detected', $6, $7::jsonb)
        ON CONFLICT (incident_id) DO NOTHING RETURNING incident_id;
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                inc_uuid,
                fingerprint,
                env,
                target_resource,
                sev,
                trigger_source,
                payload_json,
            )
            await conn.execute(
                """
                INSERT INTO incident_state_transitions (
                    incident_id, from_state, to_state, reason
                ) VALUES ($1, 'detected'::incident_lifecycle_state, 'detected'::incident_lifecycle_state, 'Initial detection')
                ON CONFLICT DO NOTHING
                """,
                inc_uuid,
            )
        return inc_id

    async def transition_state(
        self,
        incident_id: str,
        to_state: str,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        inc_uuid = _ensure_uuid(incident_id)
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM incidents WHERE incident_id = $1", inc_uuid
            )
            if not exists:
                await conn.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, fingerprint, environment, target_resource,
                        severity, current_state, trigger_source, trigger_payload
                    ) VALUES ($1, $2, 'kubernetes'::incident_environment, 'unknown', 'error'::incident_severity, $3::incident_lifecycle_state, 'fsm_auto', '{}'::jsonb)
                    ON CONFLICT (incident_id) DO NOTHING
                    """,
                    inc_uuid,
                    f"fp-{inc_uuid.hex[:8]}",
                    to_state.lower(),
                )
            row = await conn.fetchrow(
                "SELECT current_state FROM incidents WHERE incident_id = $1", inc_uuid
            )
            from_state = row["current_state"] if row else "detected"
            resolved_sql = (
                ", resolved_at = NOW()"
                if to_state in ("resolved", "rolled_back", "failed")
                else ""
            )
            await conn.execute(
                f"UPDATE incidents SET current_state = $1::incident_lifecycle_state, updated_at = NOW() {resolved_sql} WHERE incident_id = $2",
                to_state.lower(),
                inc_uuid,
            )
            await conn.execute(
                """
                INSERT INTO incident_state_transitions (
                    incident_id, from_state, to_state, reason, metadata
                ) VALUES ($1, $2::incident_lifecycle_state, $3::incident_lifecycle_state, $4, $5::jsonb)
                """,
                inc_uuid,
                from_state,
                to_state.lower(),
                reason or "",
                json.dumps(metadata or {}),
            )

    async def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        inc_uuid = _ensure_uuid(incident_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM incidents WHERE incident_id = $1", inc_uuid
            )
            if not row:
                return None
            res = dict(row)
            res["incident_id"] = str(res["incident_id"])
            if isinstance(res.get("trigger_payload"), str):
                try:
                    res["trigger_payload"] = json.loads(res["trigger_payload"])
                except Exception:
                    pass
            return res

    async def get_active_incident_by_target(
        self, target_resource: str
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM incidents WHERE target_resource = $1
                  AND current_state NOT IN ('resolved', 'rolled_back', 'rejected', 'escalated', 'failed')
                ORDER BY created_at DESC LIMIT 1
                """,
                target_resource,
            )
            if not row:
                return None
            res = dict(row)
            res["incident_id"] = str(res["incident_id"])
            if isinstance(res.get("trigger_payload"), str):
                try:
                    res["trigger_payload"] = json.loads(res["trigger_payload"])
                except Exception:
                    pass
            return res

    async def list_incidents(
        self, limit: int = 50, state: str | None = None
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            if state:
                rows = await conn.fetch(
                    "SELECT * FROM incidents WHERE current_state = $1::incident_lifecycle_state ORDER BY created_at DESC LIMIT $2",
                    state.lower(),
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM incidents ORDER BY created_at DESC LIMIT $1", limit
                )
            results = []
            for r in rows:
                d = dict(r)
                d["incident_id"] = str(d["incident_id"])
                results.append(d)
            return results

    async def update_root_cause(
        self, incident_id: str, summary: str, confidence: float
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE incidents SET root_cause_summary = $1, confidence_score = $2, updated_at = NOW() WHERE incident_id = $3",
                summary,
                confidence,
                _ensure_uuid(incident_id),
            )
