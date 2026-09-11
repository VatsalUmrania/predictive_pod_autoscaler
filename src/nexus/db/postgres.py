"""
NEXUS Unified Database Layer
=============================
Async database client supporting PostgreSQL via asyncpg with connection pooling,
exponential backoff retries, and unified schema initialization.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_uuid(val: str | None) -> uuid.UUID:
    """Ensure a value is converted to a valid UUID. If not a valid hex UUID,
    deterministically hash it via uuid5 so any cluster ID or resource string is valid."""
    if not val:
        return uuid.uuid4()
    if isinstance(val, uuid.UUID):
        return val
    try:
        return uuid.UUID(str(val))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(val))


class PostgresClient:
    """
    PostgreSQL Client using asyncpg with connection pooling and schema initialization.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv(
            "NEXUS_POSTGRES_DSN",
            os.getenv("DATABASE_URL", "")
        )
        self.min_pool = int(os.getenv("NEXUS_PG_MIN_POOL", "2"))
        self.max_pool = int(os.getenv("NEXUS_PG_MAX_POOL", "10"))
        self._pool: Any = None
        self._lock = asyncio.Lock()

    @property
    def pool(self) -> Any:
        return self._pool

    @asynccontextmanager
    async def acquire(self):
        """Helper to acquire a connection from the pool."""
        if not self._pool:
            raise RuntimeError("PostgresClient connection pool is not initialized.")
        async with self._pool.acquire() as conn:
            yield conn

    async def initialize(self, max_retries: int = 6, initial_backoff: float = 1.0) -> None:
        """Create pool with retry backoff and execute schema initialization."""
        import asyncpg

        if not self.dsn:
            raise ValueError(
                "No PostgreSQL DSN configured. Set NEXUS_POSTGRES_DSN or DATABASE_URL."
            )

        last_exc: Exception | None = None
        backoff = initial_backoff
        for attempt in range(1, max_retries + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    self.dsn,
                    min_size=self.min_pool,
                    max_size=self.max_pool,
                    command_timeout=30.0,
                )
                schema_path = Path(__file__).parent / "schema.sql"
                if schema_path.exists():
                    ddl = schema_path.read_text(encoding="utf-8")
                    async with self._pool.acquire() as conn:
                        await conn.execute(ddl)
                logger.info(
                    "[PostgresClient] Initialized connection pool (min=%d, max=%d) and verified schema.",
                    self.min_pool,
                    self.max_pool,
                )
                return
            except Exception as exc:
                last_exc = exc
                if self._pool:
                    try:
                        await self._pool.close()
                    except Exception:
                        pass
                    self._pool = None
                if attempt < max_retries:
                    wait_time = min(backoff, 10.0)
                    logger.warning(
                        "[PostgresClient] Connection attempt %d/%d failed (%s) — retrying in %.1fs...",
                        attempt,
                        max_retries,
                        exc,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    backoff *= 2
                else:
                    logger.error(
                        "[PostgresClient] All %d connection attempts failed: %s",
                        max_retries,
                        exc,
                    )

        raise RuntimeError(
            f"Failed to initialize PostgreSQL pool after {max_retries} attempts: {last_exc}"
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    # ── Incidents ─────────────────────────────────────────────────────────────

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

        # Sanitize severity and environment to match Postgres enums
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
        ON CONFLICT (incident_id) DO NOTHING
        RETURNING incident_id;
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
            # Log initial state transition safely
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
            # Ensure parent incident exists in incidents table to prevent foreign key violation
            exists = await conn.fetchval(
                "SELECT 1 FROM incidents WHERE incident_id = $1",
                inc_uuid,
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

            # Fetch current state
            row = await conn.fetchrow(
                "SELECT current_state FROM incidents WHERE incident_id = $1",
                inc_uuid,
            )
            from_state = row["current_state"] if row else "detected"

            # Update incident
            resolved_sql = ", resolved_at = NOW()" if to_state in ("resolved", "rolled_back", "failed") else ""
            await conn.execute(
                f"""
                UPDATE incidents
                SET current_state = $1::incident_lifecycle_state,
                    updated_at = NOW()
                    {resolved_sql}
                WHERE incident_id = $2
                """,
                to_state.lower(),
                inc_uuid,
            )

            # Record transition
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
                "SELECT * FROM incidents WHERE incident_id = $1",
                inc_uuid,
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
                SELECT * FROM incidents
                WHERE target_resource = $1
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
                    """
                    SELECT * FROM incidents
                    WHERE current_state = $1::incident_lifecycle_state
                    ORDER BY created_at DESC LIMIT $2
                    """,
                    state.lower(),
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM incidents ORDER BY created_at DESC LIMIT $1",
                    limit,
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
                """
                UPDATE incidents
                SET root_cause_summary = $1, confidence_score = $2, updated_at = NOW()
                WHERE incident_id = $3
                """,
                summary,
                confidence,
                _ensure_uuid(incident_id),
            )

    # ── Agent Runs & Messages ─────────────────────────────────────────────────

    async def start_agent_run(
        self, incident_id: str, agent_type: str, model_name: str
    ) -> str:
        run_id = str(uuid.uuid4())
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs (run_id, incident_id, agent_type, model_name, status)
                VALUES ($1, $2, $3, $4, 'running'::agent_run_status)
                """,
                uuid.UUID(run_id),
                _ensure_uuid(incident_id),
                agent_type,
                model_name,
            )
        return run_id

    async def finish_agent_run(
        self,
        run_id: str,
        status: str = "completed",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: int | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_runs
                SET status = $1::agent_run_status,
                    prompt_tokens = $2,
                    completion_tokens = $3,
                    total_tokens = $2 + $3,
                    duration_ms = $4,
                    error_message = $5,
                    finished_at = NOW()
                WHERE run_id = $6
                """,
                status.lower(),
                prompt_tokens,
                completion_tokens,
                duration_ms,
                error_message,
                uuid.UUID(run_id),
            )

    async def log_message(
        self,
        run_id: str,
        sequence_num: int,
        role: str,
        content: str,
        reasoning_content: str | None = None,
        raw_payload: dict | None = None,
    ) -> str:
        msg_id = str(uuid.uuid4())
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_messages (
                    message_id, run_id, sequence_num, role,
                    content, reasoning_content, raw_payload
                ) VALUES ($1, $2, $3, $4::message_role, $5, $6, $7::jsonb)
                """,
                uuid.UUID(msg_id),
                uuid.UUID(run_id),
                sequence_num,
                role.lower(),
                content,
                reasoning_content,
                json.dumps(raw_payload or {}) if raw_payload else None,
            )
        return msg_id

    # ── Tool Calls ────────────────────────────────────────────────────────────

    async def log_tool_call(
        self,
        run_id: str,
        tool_name: str,
        tool_category: str,
        input_parameters: dict,
        message_id: str | None = None,
    ) -> str:
        call_id = str(uuid.uuid4())
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_calls (
                    call_id, run_id, message_id, tool_name,
                    tool_category, input_parameters, status
                ) VALUES ($1, $2, $3, $4, $5::tool_category, $6::jsonb, 'invoked'::tool_status)
                """,
                uuid.UUID(call_id),
                uuid.UUID(run_id),
                uuid.UUID(message_id) if message_id else None,
                tool_name,
                tool_category.lower(),
                json.dumps(input_parameters or {}),
            )
        return call_id

    async def update_tool_call(
        self,
        call_id: str,
        status: str,
        output_result: dict | None = None,
        error_details: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tool_calls
                SET status = $1::tool_status,
                    output_result = $2::jsonb,
                    error_details = $3,
                    execution_duration_ms = $4
                WHERE call_id = $5
                """,
                status.lower(),
                json.dumps(output_result or {}) if output_result else None,
                error_details,
                duration_ms,
                uuid.UUID(call_id),
            )

    # ── Remediation Actions ───────────────────────────────────────────────────

    async def create_remediation_action(
        self,
        incident_id: str,
        action_name: str,
        action_level: str,
        target_resource: str,
        parameters: dict,
        run_id: str | None = None,
        rollback_plan: dict | None = None,
        pre_check_snapshot: dict | None = None,
    ) -> str:
        action_id = str(uuid.uuid4())
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO remediation_actions (
                    action_id, incident_id, run_id, action_name,
                    action_level, target_resource, parameters,
                    rollback_plan, pre_check_snapshot, outcome
                ) VALUES ($1, $2, $3, $4, $5::action_level, $6, $7::jsonb, $8::jsonb, $9::jsonb, 'pending'::action_outcome)
                """,
                uuid.UUID(action_id),
                _ensure_uuid(incident_id),
                uuid.UUID(run_id) if run_id else None,
                action_name,
                action_level,
                target_resource,
                json.dumps(parameters or {}),
                json.dumps(rollback_plan or {}) if rollback_plan else None,
                json.dumps(pre_check_snapshot or {}) if pre_check_snapshot else None,
            )
        return action_id

    async def update_remediation_action(
        self,
        action_id: str,
        outcome: str,
        post_check_snapshot: dict | None = None,
        policy_check_passed: bool | None = None,
        human_approved_by: str | None = None,
    ) -> None:
        updates = ["outcome = $1::action_outcome"]
        params: list[Any] = [outcome.lower(), uuid.UUID(action_id)]
        idx = 3

        if post_check_snapshot is not None:
            updates.append(f"post_check_snapshot = ${idx}::jsonb")
            params.insert(idx - 1, json.dumps(post_check_snapshot))
            idx += 1
        if policy_check_passed is not None:
            updates.append(f"policy_check_passed = ${idx}")
            params.insert(idx - 1, policy_check_passed)
            idx += 1
        if human_approved_by is not None:
            updates.append(f"human_approved_by = ${idx}, approval_granted_at = NOW()")
            params.insert(idx - 1, human_approved_by)
            idx += 1

        sql = f"""
        UPDATE remediation_actions
        SET {", ".join(updates)}, verified_at = NOW()
        WHERE action_id = $2
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    # ── Full Incident Trace ───────────────────────────────────────────────────

    async def get_incident_trace(self, incident_id: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            u_id = _ensure_uuid(incident_id)
            inc = await conn.fetchrow("SELECT * FROM incidents WHERE incident_id = $1", u_id)
            if not inc:
                return {}
            transitions = await conn.fetch(
                "SELECT * FROM incident_state_transitions WHERE incident_id = $1 ORDER BY created_at ASC",
                u_id,
            )
            runs = await conn.fetch(
                "SELECT * FROM agent_runs WHERE incident_id = $1 ORDER BY created_at ASC",
                u_id,
            )
            actions = await conn.fetch(
                "SELECT * FROM remediation_actions WHERE incident_id = $1 ORDER BY created_at ASC",
                u_id,
            )

            run_dicts = []
            for r in runs:
                rd = dict(r)
                r_id = rd["run_id"]
                messages = await conn.fetch(
                    "SELECT * FROM agent_messages WHERE run_id = $1 ORDER BY sequence_num ASC",
                    r_id,
                )
                tools = await conn.fetch(
                    "SELECT * FROM tool_calls WHERE run_id = $1 ORDER BY created_at ASC",
                    r_id,
                )
                rd["run_id"] = str(r_id)
                rd["messages"] = [dict(m) for m in messages]
                rd["tool_calls"] = [dict(t) for t in tools]
                run_dicts.append(rd)

            inc_dict = dict(inc)
            inc_dict["incident_id"] = str(inc_dict["incident_id"])
            return {
                "incident": inc_dict,
                "state_transitions": [dict(t) for t in transitions],
                "agent_runs": run_dicts,
                "remediation_actions": [dict(a) for a in actions],
            }


# Global database factory
_global_db_client: PostgresClient | None = None


async def get_database_client(dsn: str | None = None) -> PostgresClient:
    """
    Factory returning the global PostgresClient singleton.
    Initializes with retry and backoff; raises RuntimeError on failure.
    """
    global _global_db_client
    if _global_db_client is not None:
        return _global_db_client

    configured_dsn = dsn or os.getenv("NEXUS_POSTGRES_DSN", os.getenv("DATABASE_URL", ""))
    if not configured_dsn:
        raise RuntimeError(
            "No PostgreSQL DSN configured. NEXUS requires PostgreSQL (set NEXUS_POSTGRES_DSN or DATABASE_URL)."
        )

    client = PostgresClient(dsn=configured_dsn)
    await client.initialize()
    _global_db_client = client
    return client

