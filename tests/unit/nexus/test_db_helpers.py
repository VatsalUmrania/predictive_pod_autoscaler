"""
Test helpers and in-memory mock for PostgreSQL database testing.
Provides MockPostgresClient for unit tests that need an offline database client.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid
from typing import Any


class MockAsyncpgConnection:
    """Mock connection for acquire() simulating asyncpg connection and queries."""

    def __init__(self, client: MockPostgresClient):
        self.client = client

    async def execute(self, query: str, *args):
        q = query.strip().upper()
        if "INSERT INTO AUDIT_TRAIL" in q:
            # (action_id, timestamp, triggered_by, runbook_id, healing_level, target, pre_check_results, execution_outcome, post_check_results, rollback_triggered, incident_id, action_results)
            rec = {
                "action_id": str(args[0]),
                "timestamp": args[1],
                "triggered_by": args[2],
                "runbook_id": args[3],
                "healing_level": args[4],
                "target": args[5],
                "pre_check_results": args[6],
                "execution_outcome": args[7],
                "post_check_results": args[8],
                "rollback_triggered": args[9],
                "incident_id": args[10],
                "action_results": args[11],
            }
            self.client._audit_records.append(rec)
            return "INSERT 0 1"

        elif "UPDATE AUDIT_TRAIL" in q:
            # SET execution_outcome = $1, post_check_results = $2, rollback_triggered = $3, action_results = $4 WHERE action_id = $5
            outcome = args[0]
            post_checks = args[1]
            rb = args[2]
            action_results = args[3]
            action_id = str(args[4])
            for r in self.client._audit_records:
                if str(r["action_id"]) == action_id:
                    r["execution_outcome"] = outcome
                    r["post_check_results"] = post_checks
                    r["rollback_triggered"] = rb
                    r["action_results"] = action_results
            return "UPDATE 1"

        elif "INSERT INTO COOLDOWNS" in q:
            key = args[0]
            expires_at = args[1]
            self.client._cooldowns[key] = expires_at
            return "INSERT 0 1"

        elif "DELETE FROM COOLDOWNS" in q:
            key = args[0]
            self.client._cooldowns.pop(key, None)
            return "DELETE 1"

        elif "INSERT INTO DEVELOPER_INCIDENTS" in q:
            rec = {
                "id": len(self.client._developer_incidents) + 1,
                "incident_id": args[0],
                "runbook_id": args[1],
                "target": args[2],
                "level": args[3],
                "outcome": args[4],
                "description": args[5],
                "confidence": args[6],
                "timestamp": args[7],
                "rca": args[8],
                "accepted_by": args[9],
                "accepted_at": args[10],
            }
            self.client._developer_incidents.append(rec)
            return "INSERT 0 1"

        elif "DELETE FROM DEVELOPER_INCIDENTS" in q:
            return "DELETE"

        return "EXECUTE 1"

    async def fetch(self, query: str, *args):
        q = query.strip().upper()
        if "FROM AUDIT_TRAIL" in q:
            if "WHERE INCIDENT_ID = $1" in q:
                inc_id = str(args[0])
                return [r for r in self.client._audit_records if r["incident_id"] == inc_id]
            elif "WHERE RUNBOOK_ID = $1" in q:
                rb_id = str(args[0])
                return [r for r in self.client._audit_records if r["runbook_id"] == rb_id]
            elif "LIMIT $1" in q:
                limit = args[0]
                return list(reversed(self.client._audit_records))[:limit]
            return list(self.client._audit_records)

        elif "FROM COOLDOWNS" in q:
            cutoff = args[0] if args else 0.0
            return [
                {"key": k, "expires_at": exp}
                for k, exp in self.client._cooldowns.items()
                if exp > cutoff
            ]

        elif "FROM DEVELOPER_INCIDENTS" in q:
            limit = args[0] if args else 200
            return list(reversed(self.client._developer_incidents))[:limit]

        return []

    async def fetchrow(self, query: str, *args):
        q = query.strip().upper()
        if "FROM COOLDOWNS WHERE KEY = $1" in q:
            key = args[0]
            if key in self.client._cooldowns:
                return {"key": key, "expires_at": self.client._cooldowns[key]}
            return None
        return None

    async def fetchval(self, query: str, *args):
        row = await self.fetchrow(query, *args)
        if row:
            return list(row.values())[0]
        return None


class MockPostgresClient:
    """
    In-memory mock database client implementing PostgresClient's interface
    for isolated unit tests without requiring a running PostgreSQL server.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or "postgresql://mock:mock@localhost:5432/mock"
        self._incidents: dict[str, dict[str, Any]] = {}
        self._transitions: list[dict[str, Any]] = []
        self._agent_runs: dict[str, dict[str, Any]] = {}
        self._messages: list[dict[str, Any]] = []
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._actions: dict[str, dict[str, Any]] = {}
        self._cooldowns: dict[str, float] = {}
        self._audit_records: list[dict[str, Any]] = []
        self._developer_incidents: list[dict[str, Any]] = []

    async def initialize(self, *args, **kwargs) -> None:
        pass

    async def close(self) -> None:
        pass

    @asynccontextmanager
    async def acquire(self):
        yield MockAsyncpgConnection(self)

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
        inc_id = incident_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._incidents[inc_id] = {
            "incident_id": inc_id,
            "fingerprint": fingerprint,
            "environment": environment,
            "target_resource": target_resource,
            "severity": severity,
            "current_state": "detected",
            "trigger_source": trigger_source,
            "trigger_payload": trigger_payload or {},
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
            "root_cause_summary": None,
            "confidence_score": None,
        }
        self._transitions.append({
            "incident_id": inc_id,
            "from_state": "detected",
            "to_state": "detected",
            "reason": "Initial detection",
            "metadata": {},
            "created_at": now,
        })
        return inc_id

    async def transition_state(
        self,
        incident_id: str,
        to_state: str,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        inc = self._incidents.get(incident_id)
        from_state = inc["current_state"] if inc else "detected"

        if inc:
            inc["current_state"] = to_state
            inc["updated_at"] = now
            if to_state in ("resolved", "failed", "rolled_back"):
                inc["resolved_at"] = now

        self._transitions.append({
            "incident_id": incident_id,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason or "",
            "metadata": metadata or {},
            "created_at": now,
        })

    async def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        return self._incidents.get(incident_id)

    async def get_active_incident_by_target(self, target_resource: str) -> dict[str, Any] | None:
        for inc in reversed(list(self._incidents.values())):
            if inc["target_resource"] == target_resource and inc["current_state"] not in (
                "resolved", "rolled_back", "rejected", "escalated", "failed"
            ):
                return inc
        return None

    async def list_incidents(self, limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
        res = list(self._incidents.values())
        if state:
            res = [i for i in res if i["current_state"] == state]
        return res[:limit]

    async def update_root_cause(self, incident_id: str, summary: str, confidence: float) -> None:
        if incident_id in self._incidents:
            self._incidents[incident_id]["root_cause_summary"] = summary
            self._incidents[incident_id]["confidence_score"] = confidence

    async def start_agent_run(
        self, incident_id: str, agent_type: str, model_name: str, run_id: str | None = None
    ) -> str:
        r_id = run_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._agent_runs[r_id] = {
            "run_id": r_id,
            "incident_id": incident_id,
            "agent_type": agent_type,
            "model_name": model_name,
            "status": "running",
            "created_at": now,
        }
        return r_id

    async def finish_agent_run(
        self,
        run_id: str,
        status: str = "completed",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        duration_ms: int | None = None,
        error_message: str | None = None,
    ) -> None:
        if run_id in self._agent_runs:
            r = self._agent_runs[run_id]
            r["status"] = status
            r["prompt_tokens"] = prompt_tokens
            r["completion_tokens"] = completion_tokens
            r["total_tokens"] = prompt_tokens + completion_tokens
            r["duration_ms"] = duration_ms
            r["error_message"] = error_message
            r["finished_at"] = datetime.now(timezone.utc).isoformat()

    async def log_message(
        self,
        run_id: str,
        sequence_num: int,
        role: str,
        content: str | None = None,
        reasoning_content: str | None = None,
        raw_payload: dict | None = None,
    ) -> str:
        msg_id = str(uuid.uuid4())
        self._messages.append({
            "message_id": msg_id,
            "run_id": run_id,
            "sequence_num": sequence_num,
            "role": role,
            "content": content,
            "reasoning_content": reasoning_content,
            "raw_payload": raw_payload,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return msg_id

    async def log_tool_call(
        self,
        run_id: str,
        tool_name: str,
        tool_category: str,
        input_parameters: dict | None = None,
        message_id: str | None = None,
        call_id: str | None = None,
    ) -> str:
        c_id = call_id or str(uuid.uuid4())
        self._tool_calls[c_id] = {
            "call_id": c_id,
            "run_id": run_id,
            "message_id": message_id,
            "tool_name": tool_name,
            "tool_category": tool_category,
            "input_parameters": input_parameters or {},
            "status": "invoked",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return c_id

    async def update_tool_call(
        self,
        call_id: str,
        status: str,
        output_result: dict | None = None,
        error_details: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if call_id in self._tool_calls:
            t = self._tool_calls[call_id]
            t["status"] = status
            t["output_result"] = output_result
            t["error_details"] = error_details
            t["execution_duration_ms"] = duration_ms

    async def create_remediation_action(
        self,
        incident_id: str,
        action_name: str,
        action_level: str,
        target_resource: str,
        parameters: dict | None = None,
        run_id: str | None = None,
        rollback_plan: dict | None = None,
        pre_check_snapshot: dict | None = None,
        action_id: str | None = None,
    ) -> str:
        a_id = action_id or str(uuid.uuid4())
        self._actions[a_id] = {
            "action_id": a_id,
            "incident_id": incident_id,
            "run_id": run_id,
            "action_name": action_name,
            "action_level": action_level,
            "target_resource": target_resource,
            "parameters": parameters or {},
            "rollback_plan": rollback_plan,
            "pre_check_snapshot": pre_check_snapshot,
            "outcome": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "policy_check_passed": False,
            "human_approved_by": None,
        }
        return a_id

    async def update_remediation_action(
        self,
        action_id: str,
        outcome: str,
        post_check_snapshot: dict | None = None,
        policy_check_passed: bool | None = None,
        human_approved_by: str | None = None,
        **kwargs,
    ) -> None:
        if action_id in self._actions:
            a = self._actions[action_id]
            a["outcome"] = outcome
            if post_check_snapshot is not None:
                a["post_check_snapshot"] = post_check_snapshot
            if policy_check_passed is not None:
                a["policy_check_passed"] = policy_check_passed
            if human_approved_by is not None:
                a["human_approved_by"] = human_approved_by

    async def get_incident_trace(self, incident_id: str) -> dict[str, Any]:
        inc = self._incidents.get(incident_id)
        transitions = [t for t in self._transitions if t["incident_id"] == incident_id]
        runs = [r for r in self._agent_runs.values() if r["incident_id"] == incident_id]
        actions = [a for a in self._actions.values() if a["incident_id"] == incident_id]
        for r in runs:
            r["messages"] = [m for m in self._messages if m["run_id"] == r["run_id"]]
            r["tool_calls"] = [t for t in self._tool_calls.values() if t["run_id"] == r["run_id"]]
        return {
            "incident": inc,
            "state_transitions": transitions,
            "agent_runs": runs,
            "remediation_actions": actions,
        }
