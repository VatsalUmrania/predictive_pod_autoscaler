"""Incident database operations repository."""

from __future__ import annotations

import json
import uuid
from typing import Any

from nexus.db.models import (
    VALID_INCIDENT_TRANSITIONS,
    VALID_TRANSITIONS,
    IncidentFSM,
    IncidentState,
    IncidentStatus,
)
from nexus.db.pool import PostgresPool
from nexus.db.utils import _ensure_uuid


class IncidentRepository:
    """Handles CRUD queries for incidents, transactional FSM, and idempotency."""

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
        workload_urn: str | None = None,
        status: IncidentStatus | str = IncidentStatus.RECEIVED,
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

        stat_enum = (
            status if isinstance(status, IncidentStatus) else IncidentStatus(status)
        )
        urn = workload_urn or target_resource

        sql = """
        INSERT INTO incidents (
            incident_id, fingerprint, environment, target_resource,
            severity, current_state, trigger_source, trigger_payload,
            workload_urn, status
        ) VALUES (
            $1, $2, $3::incident_environment, $4,
            $5::incident_severity, 'detected', $6, $7::jsonb,
            $8, $9::incident_status
        )
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
                urn,
                stat_enum.value,
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

    async def create_or_coalesce_incident(
        self,
        *,
        workload_urn: str,
        fingerprint: str,
        environment: str = "kubernetes",
        severity: str = "error",
        trigger_source: str = "otlp",
        trigger_payload: dict | None = None,
        incident_id: str | None = None,
    ) -> tuple[str, bool]:
        """Single-flight coalescing: insert or coalesce if active mutation pipeline exists."""
        async with self._pool.acquire() as conn:
            active = await conn.fetchrow(
                """
                SELECT incident_id, status FROM incidents
                WHERE workload_urn = $1
                  AND status IN ('INVESTIGATING', 'PLANNED', 'VALIDATING', 'AWAITING_APPROVAL', 'APPROVED', 'EXECUTING')
                ORDER BY created_at DESC LIMIT 1
                """,
                workload_urn,
            )
            if active:
                return str(active["incident_id"]), True

        try:
            new_id = await self.create_incident(
                fingerprint=fingerprint,
                environment=environment,
                target_resource=workload_urn,
                severity=severity,
                trigger_source=trigger_source,
                trigger_payload=trigger_payload,
                incident_id=incident_id,
                workload_urn=workload_urn,
                status=IncidentStatus.RECEIVED,
            )
            return new_id, False
        except Exception:
            # Concurrency race on idx_active_incident_per_urn
            async with self._pool.acquire() as conn:
                active = await conn.fetchrow(
                    """
                    SELECT incident_id FROM incidents
                    WHERE workload_urn = $1
                      AND status IN ('INVESTIGATING', 'PLANNED', 'VALIDATING', 'AWAITING_APPROVAL', 'APPROVED', 'EXECUTING')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    workload_urn,
                )
                if active:
                    return str(active["incident_id"]), True
                raise

    async def claim_execution_idempotent(
        self, incident_id: str, plan_hash: str
    ) -> tuple[bool, str | None]:
        """Idempotently claim execution by updating status='APPROVED' and plan_hash=$2."""
        inc_uuid = _ensure_uuid(incident_id)
        execution_id = str(uuid.uuid4())
        sql = """
        UPDATE incidents
        SET status = 'EXECUTING'::incident_status,
            current_state = 'executing'::incident_lifecycle_state,
            execution_id = $1,
            updated_at = NOW()
        WHERE (incident_id = $2 OR incident_id::varchar = $3)
          AND (status = 'APPROVED'::incident_status OR current_state = 'approval_pending')
          AND plan_hash = $4
        RETURNING incident_id, execution_id;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql, execution_id, inc_uuid, str(inc_uuid), plan_hash
            )
            if not row:
                return False, None
            return True, execution_id

    async def transition_status(
        self,
        incident_id: str,
        to_status: IncidentStatus | str,
        plan_hash: str | None = None,
        reason: str = "",
    ) -> bool:
        """Strict transactional FSM transition in PostgreSQL."""
        inc_uuid = _ensure_uuid(incident_id)
        stat = (
            to_status
            if isinstance(to_status, IncidentStatus)
            else IncidentStatus(to_status)
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, current_state FROM incidents WHERE incident_id = $1",
                inc_uuid,
            )
            if not row:
                return False
            cur_stat = IncidentStatus(row["status"] or "RECEIVED")
            allowed = VALID_INCIDENT_TRANSITIONS.get(cur_stat, set())
            if stat not in allowed and stat != cur_stat:
                return False

            plan_sql = ", plan_hash = $3" if plan_hash is not None else ""
            resolved_sql = (
                ", resolved_at = NOW()"
                if stat in (IncidentStatus.RESOLVED, IncidentStatus.ROLLED_BACK)
                else ""
            )
            args: list[Any] = [stat.value, inc_uuid]
            if plan_hash is not None:
                args.insert(2, plan_hash)

            update_sql = f"""
            UPDATE incidents
            SET status = $1::incident_status,
                updated_at = NOW()
                {plan_sql}
                {resolved_sql}
            WHERE incident_id = $2
            """
            await conn.execute(update_sql, *args)
            return True

    async def transition_state(
        self,
        incident_id: str,
        to_state: str,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        inc_uuid = _ensure_uuid(incident_id)
        to_stat = IncidentStatus(to_state)
        async with self._pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM incidents WHERE incident_id = $1", inc_uuid
            )
            if not exists:
                await conn.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, fingerprint, environment, target_resource,
                        severity, current_state, status, trigger_source, trigger_payload
                    ) VALUES ($1, $2, 'kubernetes'::incident_environment, 'unknown', 'error'::incident_severity, $3::incident_lifecycle_state, $4::incident_status, 'fsm_auto', '{}'::jsonb)
                    ON CONFLICT (incident_id) DO NOTHING
                    """,
                    inc_uuid,
                    f"fp-{inc_uuid.hex[:8]}",
                    to_state.lower(),
                    to_stat.value,
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
                f"UPDATE incidents SET current_state = $1::incident_lifecycle_state, status = $2::incident_status, updated_at = NOW() {resolved_sql} WHERE incident_id = $3",
                to_state.lower(),
                to_stat.value,
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
                SELECT * FROM incidents WHERE (target_resource = $1 OR workload_urn = $1)
                  AND status NOT IN ('RESOLVED', 'ROLLED_BACK', 'REJECTED', 'EXPIRED', 'EXECUTION_FAILED', 'VALIDATION_FAILED')
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
                stat = IncidentStatus(state)
                rows = await conn.fetch(
                    "SELECT * FROM incidents WHERE (status = $1::incident_status OR current_state = $2::incident_lifecycle_state) ORDER BY created_at DESC LIMIT $3",
                    stat.value,
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

    async def update_discovered_target(
        self,
        incident_id: str,
        *,
        target_resource: str,
        namespace: str | None = None,
        resource_kind: str | None = None,
        service_name: str | None = None,
        failure_class: str | None = None,
        root_cause_summary: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Persist dynamically discovered target identity and diagnosis metadata to PostgreSQL."""
        inc_uuid = _ensure_uuid(incident_id)
        metadata_patch = {
            "discovered_namespace": namespace,
            "discovered_resource_kind": resource_kind,
            "discovered_service_name": service_name,
            "discovered_target_resource": target_resource,
            "failure_class": failure_class,
        }
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE incidents
                SET target_resource = COALESCE($1, target_resource),
                    workload_urn = COALESCE($1, workload_urn),
                    root_cause_summary = COALESCE($2, root_cause_summary),
                    confidence_score = COALESCE($3, confidence_score),
                    trigger_payload = COALESCE(trigger_payload, '{}'::jsonb) || $4::jsonb,
                    updated_at = NOW()
                WHERE incident_id = $5
                """,
                target_resource,
                root_cause_summary,
                confidence,
                json.dumps(metadata_patch),
                inc_uuid,
            )


__all__ = [
    "IncidentRepository",
    "IncidentStatus",
    "IncidentState",
    "VALID_INCIDENT_TRANSITIONS",
    "VALID_TRANSITIONS",
    "IncidentFSM",
]
