"""Remediation actions and trace aggregation repository."""

from __future__ import annotations

import json
import uuid
from typing import Any

from nexus.core.database.connection.pool import PostgresPool
from nexus.core.database.connection.utils import _ensure_uuid


class RemediationRepository:
    """Handles remediation_actions table and full incident trace queries."""

    def __init__(self, pool: PostgresPool):
        self._pool = pool

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

        sql = f"UPDATE remediation_actions SET {', '.join(updates)}, verified_at = NOW() WHERE action_id = $2"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *params)

    async def get_incident_trace(self, incident_id: str) -> dict[str, Any]:
        async with self._pool.acquire() as conn:
            u_id = _ensure_uuid(incident_id)
            inc = await conn.fetchrow(
                "SELECT * FROM incidents WHERE incident_id = $1", u_id
            )
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
