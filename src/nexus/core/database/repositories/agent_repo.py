"""Agent runs, messages, and tool calls database repository."""

from __future__ import annotations

import json
import uuid
from typing import Any

from nexus.core.database.connection.pool import PostgresPool
from nexus.core.database.connection.utils import _ensure_uuid


def _iso(value: Any) -> str | None:
    """Render a TIMESTAMPTZ column as an ISO-8601 string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _agent_run_row(row: Any) -> dict[str, Any]:
    """Normalize one agent_runs row into the dashboard's agent-run shape."""
    d = dict(row)
    agent_type = str(d.get("agent_type") or "")
    return {
        "run_id": str(d.get("run_id")),
        "incident_id": str(d.get("incident_id")),
        # agent_runs has no node_name column; agent_type carries the graph node.
        "node_name": agent_type or None,
        "agent_type": agent_type,
        "model_name": str(d.get("model_name") or ""),
        "status": str(d.get("status") or ""),
        "prompt_tokens": d.get("prompt_tokens"),
        "completion_tokens": d.get("completion_tokens"),
        "total_tokens": d.get("total_tokens"),
        "duration_ms": d.get("duration_ms"),
        "error_message": d.get("error_message"),
        "created_at": _iso(d.get("created_at")) or "",
        "finished_at": _iso(d.get("finished_at")),
    }


def _agent_message_row(row: Any) -> dict[str, Any]:
    """Normalize one agent_messages row into the dashboard's message shape."""
    d = dict(row)
    raw_payload = d.get("raw_payload")
    if isinstance(raw_payload, str):
        try:
            raw_payload = json.loads(raw_payload)
        except Exception:
            raw_payload = None
    return {
        "message_id": str(d.get("message_id")),
        "run_id": str(d.get("run_id")),
        # agent_messages has no incident_id column; it comes from the parent run.
        "incident_id": str(d.get("parent_incident_id") or d.get("incident_id") or ""),
        "sequence_num": int(d.get("sequence_num") or 0),
        "role": str(d.get("role") or ""),
        "content": d.get("content"),
        "reasoning_content": d.get("reasoning_content"),
        "raw_payload": raw_payload,
        "created_at": _iso(d.get("created_at")) or "",
    }


class AgentRepository:
    """Handles agent_runs, agent_messages, and tool_calls tables."""

    def __init__(self, pool: PostgresPool):
        self._pool = pool

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
                SET status = $1::agent_run_status, prompt_tokens = $2, completion_tokens = $3,
                    total_tokens = $2 + $3, duration_ms = $4, error_message = $5, finished_at = NOW()
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
                    message_id, run_id, sequence_num, role, content, reasoning_content, raw_payload
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
                    call_id, run_id, message_id, tool_name, tool_category, input_parameters, status
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
                SET status = $1::tool_status, output_result = $2::jsonb,
                    error_details = $3, execution_duration_ms = $4
                WHERE call_id = $5
                """,
                status.lower(),
                json.dumps(output_result or {}) if output_result else None,
                error_details,
                duration_ms,
                uuid.UUID(call_id),
            )

    async def list_agent_runs(self, incident_id: str) -> list[dict[str, Any]]:
        """Return the agent_runs rows for one incident, oldest first."""
        u_id = _ensure_uuid(incident_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_runs WHERE incident_id = $1 ORDER BY created_at ASC",
                u_id,
            )
        return [_agent_run_row(r) for r in rows]

    async def list_agent_messages(self, incident_id: str) -> list[dict[str, Any]]:
        """Return every agent_message belonging to one incident's runs.

        Messages are reached through their parent run, so this joins rather than
        issuing one query per run.
        """
        u_id = _ensure_uuid(incident_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.*, r.incident_id AS parent_incident_id
                FROM agent_messages m
                JOIN agent_runs r ON r.run_id = m.run_id
                WHERE r.incident_id = $1
                ORDER BY r.created_at ASC, m.sequence_num ASC
                """,
                u_id,
            )
        return [_agent_message_row(r) for r in rows]
