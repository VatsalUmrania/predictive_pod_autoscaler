"""
NEXUS Demo Orchestrator — Reasoning Loop
==========================================
A lightweight sense→reason→act→verify loop for the demo application.
Runs as a standalone asyncio process alongside the demo backend.

What this demonstrates:
  1. SENSE   — Subscribes to NATS, receives IncidentEvents from SDK + agents
  2. REASON  — Rule-based RCA (maps signal type → failure class → runbook)
               Optionally uses Gemini 1.5 Flash if NEXUS_GEMINI_API_KEY is set
  3. ACT     — Calls /inject/heal-all on demo backend to reset failure state
               Writes healing record to AuditTrail SQLite
  4. VERIFY  — Polls /health on demo backend to confirm recovery
  5. LEARN   — Publishes outcome to nexus.actions.healed (read by console)

RCA mapping (rule-based):
  route_error + sdk.*          → bad_deploy     → "Rollback deployment"
  db_query (slow/spike)        → db_pressure    → "Reset connection pool"
  env_contract_violation       → config_error   → "Block deploy + alert"
  secret_committed             → security       → "Revoke + rotate secret"
  dns_error_injected           → network_error  → "DNS flush + retry"
  oom_injected / memory        → resource_exhaust → "Restart + increase limits"

Usage:
    python -m demo.backend.demo_orchestrator
    # or via docker-compose service
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import nats

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [Orchestrator] %(levelname)s %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("nexus.demo_orchestrator")

# ── Config ────────────────────────────────────────────────────────────────────
NATS_URL      = os.getenv("NATS_URL",        "nats://localhost:4222")
NEXUS_API_URL = os.getenv("NEXUS_API_URL",   "http://localhost:8080")
DEMO_API_URL  = os.getenv("DEMO_API_URL",    "http://localhost:8001")
AUDIT_DB_PATH = os.getenv("NEXUS_AUDIT_DB_PATH", "data/nexus_audit.db")
GEMINI_KEY    = os.getenv("NEXUS_GEMINI_API_KEY", "")

# How many events to accumulate before triggering analysis
CORRELATION_WINDOW_S = 15   # seconds
MIN_EVENTS_TO_TRIGGER = 2   # at least 2 events before acting


# ── RCA rule table ────────────────────────────────────────────────────────────

_RCA_RULES = [
    {
        "signals":    ["route_error", "error_rate_injected"],
        "class":      "bad_deploy",
        "runbook":    "runbook_high_error_rate_post_deploy_v1",
        "level":      2,
        "confidence": 0.88,
        "action":     "rollback_deployment",
        "description":"High error rate detected after recent activity. Rolling back to last stable state.",
    },
    {
        "signals":    ["db_query", "db_spike_injected"],
        "class":      "db_pressure",
        "runbook":    "runbook_db_connection_exhaustion_v1",
        "level":      1,
        "confidence": 0.82,
        "action":     "reset_connection_pool",
        "description":"Database query spike detected. Resetting connection pool and adjusting limits.",
    },
    {
        "signals":    ["env_contract_violation", "missing_env"],
        "class":      "config_error",
        "runbook":    "runbook_missing_env_key_v1",
        "level":      0,
        "confidence": 0.99,
        "action":     "block_deploy",
        "description":"Missing environment variables detected. Blocking deployment until resolved.",
    },
    {
        "signals":    ["secret_committed"],
        "class":      "security_violation",
        "runbook":    "runbook_secret_leaked_v1",
        "level":      3,
        "confidence": 0.97,
        "action":     "revoke_and_alert",
        "description":"Credential leaked in source code. Revoking token and alerting security team.",
    },
    {
        "signals":    ["dns_error_injected", "failed_fetch"],
        "class":      "network_error",
        "runbook":    "runbook_dns_resolution_failure_v1",
        "level":      1,
        "confidence": 0.79,
        "action":     "dns_flush",
        "description":"DNS resolution failure detected. Flushing DNS cache and retrying upstream.",
    },
    {
        "signals":    ["oom_injected", "function_error"],
        "class":      "resource_exhaustion",
        "runbook":    "runbook_pod_crashloop_v1",
        "level":      1,
        "confidence": 0.85,
        "action":     "restart_and_resize",
        "description":"Memory pressure detected. Restarting service with increased memory limits.",
    },
    {
        "signals":    ["config_drift_detected", "bad_config"],
        "class":      "config_drift",
        "runbook":    "runbook_missing_env_key_v1",
        "level":      0,
        "confidence": 0.91,
        "action":     "revert_config",
        "description":"Configuration drift detected. Reverting to last known-good config.",
    },
    {
        "signals":    ["code_quality_warning"],
        "class":      "code_risk",
        "runbook":    "runbook_high_error_rate_post_deploy_v1",
        "level":      0,
        "confidence": 0.70,
        "action":     "flag_for_review",
        "description":"Code quality issues detected in the push. Flagging for human review.",
    },
]


def _match_rule(event_types: List[str]) -> Optional[Dict]:
    """Return the first matching RCA rule for the observed event types."""
    for rule in _RCA_RULES:
        if any(sig in event_types for sig in rule["signals"]):
            return rule
    return None


# ── Gemini RCA (optional enhancement) ────────────────────────────────────────

async def _gemini_rca(events: List[Dict]) -> Optional[Dict]:
    """Call Gemini 1.5 Flash for enhanced RCA (only if API key is set)."""
    if not GEMINI_KEY:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_KEY)
        model  = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "You are a Kubernetes SRE analyzing infrastructure incidents.\n"
            f"Events observed (last 15 seconds): {json.dumps(events[:5], indent=2)}\n\n"
            "Return a JSON object with keys: failure_class, confidence (0-1), "
            "suggested_action, plain_english_summary. Be concise."
        )
        resp   = await asyncio.to_thread(model.generate_content, prompt)
        text   = resp.text.strip()
        # Extract JSON from response
        start  = text.find("{")
        end    = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as exc:
        logger.debug(f"Gemini RCA skipped: {exc}")
    return None


# ── AuditTrail write ──────────────────────────────────────────────────────────

async def _write_audit(
    incident_id:   str,
    runbook_id:    str,
    target:        str,
    level:         int,
    outcome:       str,
    description:   str,
    confidence:    float,
) -> None:
    try:
        import aiosqlite
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(AUDIT_DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS audit_records (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id      TEXT,
                    runbook_id       TEXT,
                    target           TEXT,
                    healing_level    INTEGER,
                    execution_outcome TEXT,
                    description      TEXT,
                    confidence       REAL,
                    timestamp        TEXT
                )
            """)
            await db.execute(
                """INSERT INTO audit_records
                   (incident_id, runbook_id, target, healing_level,
                    execution_outcome, description, confidence, timestamp)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (incident_id, runbook_id, target, level, outcome, description, confidence, now),
            )
            await db.commit()
        logger.info(f"AuditTrail: {outcome} — {description[:60]}")
    except Exception as exc:
        logger.debug(f"AuditTrail write skipped: {exc}")


# ── Healing executor ──────────────────────────────────────────────────────────

async def _execute_healing(rule: Dict, incident_id: str, gemini_result: Optional[Dict]) -> str:
    """
    Demo-mode healing execution:
    1. Log what NEXUS would do in production
    2. Call /inject/heal-all to reset demo failure state
    3. Verify backend health
    4. Return outcome
    """
    description = (
        gemini_result.get("plain_english_summary", rule["description"])
        if gemini_result else rule["description"]
    )

    logger.info(f"🔧 HEALING [{rule['class']}] → {rule['action']}: {description}")

    # Simulate healing delay (realistic)
    await asyncio.sleep(2.0)

    # Reset demo backend failure state
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{DEMO_API_URL}/inject/heal-all")
        logger.info("✅ Demo backend failure state reset")
    except Exception as exc:
        logger.warning(f"Could not reset demo backend: {exc}")

    # Verify recovery (post-check)
    await asyncio.sleep(1.0)
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{DEMO_API_URL}/health")
            health = r.json()
            outcome = "success" if r.status_code == 200 else "failed"
    except Exception:
        outcome = "failed"

    # Write to AuditTrail
    await _write_audit(
        incident_id  = incident_id,
        runbook_id   = rule["runbook"],
        target       = "shop-demo",
        level        = rule["level"],
        outcome      = outcome,
        description  = description,
        confidence   = rule["confidence"],
    )

    # Report outcome to NEXUS SDK endpoint
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(f"{NEXUS_API_URL}/sdk/event", json={
                "type":        "healing_completed",
                "app":         "shop-demo",
                "incident_id": incident_id,
                "runbook":     rule["runbook"],
                "outcome":     outcome,
                "action":      rule["action"],
                "description": description,
                "confidence":  rule["confidence"],
            }, headers={"Authorization": f"Bearer {os.environ.get('SELFHEAL_TOKEN','')}"})
    except Exception:
        pass

    return outcome


# ── Main correlation loop ─────────────────────────────────────────────────────

class DemoOrchestrator:
    def __init__(self) -> None:
        self._event_buffer: List[Dict]     = []
        self._last_flush:   float          = time.monotonic()
        self._healing:      bool           = False   # prevent concurrent healing
        self._nc:           Any            = None
        self._incident_seq: int            = 0

    async def start(self) -> None:
        logger.info(f"Connecting to NATS at {NATS_URL}…")
        self._nc = await nats.connect(NATS_URL)
        logger.info("✅ NATS connected")

        # Subscribe to all incident subjects
        await self._nc.subscribe("nexus.incidents.*",     cb=self._on_event)
        await self._nc.subscribe("nexus.incidents.sdk.*", cb=self._on_event)
        logger.info("📡 Subscribed to nexus.incidents.* + nexus.incidents.sdk.*")
        logger.info("🧠 Demo Orchestrator is running — sense→reason→act loop active")

        # Run correlation flush loop
        while True:
            await asyncio.sleep(1)
            elapsed = time.monotonic() - self._last_flush
            if (elapsed >= CORRELATION_WINDOW_S
                    and len(self._event_buffer) >= MIN_EVENTS_TO_TRIGGER
                    and not self._healing):
                await self._flush()

    async def _on_event(self, msg: Any) -> None:
        try:
            data       = json.loads(msg.data.decode())
            event_type = data.get("type") or msg.subject.split(".")[-1]
            self._event_buffer.append({
                "type":     event_type,
                "subject":  msg.subject,
                "data":     data,
                "received": time.monotonic(),
            })
            logger.info(f"📥 Event: {event_type} (buffer={len(self._event_buffer)})")

            # Immediate trigger for high-severity events
            if event_type in ("secret_committed", "env_contract_violation") and not self._healing:
                await self._flush()
        except Exception as exc:
            logger.debug(f"Event parse error: {exc}")

    async def _flush(self) -> None:
        if not self._event_buffer:
            return

        self._healing = True
        events        = list(self._event_buffer)
        self._event_buffer.clear()
        self._last_flush = time.monotonic()

        event_types = [e["type"] for e in events]
        logger.info(f"\n{'='*50}")
        logger.info(f"🔍 CORRELATING {len(events)} events: {event_types}")

        # RCA — try Gemini first, fall back to rules
        gemini_result = await _gemini_rca(events)
        rule          = _match_rule(event_types)

        if not rule:
            logger.info("ℹ️  No matching rule — events logged, no action taken")
            self._healing = False
            return

        confidence = rule["confidence"]
        if gemini_result:
            confidence = min(0.99, confidence * 1.1)  # Gemini boosts confidence slightly
            logger.info(f"🤖 Gemini RCA: {gemini_result.get('failure_class')} ({gemini_result.get('confidence'):.0%})")

        self._incident_seq += 1
        incident_id = f"INC-{self._incident_seq:04d}-{int(time.time())}"

        logger.info(f"🎯 RCA: class={rule['class']} runbook={rule['runbook']} confidence={confidence:.0%}")
        logger.info(f"📋 Action: {rule['action']} — {rule['description'][:70]}")

        outcome = await _execute_healing(rule, incident_id, gemini_result)

        logger.info(f"{'✅' if outcome == 'success' else '❌'} Incident {incident_id}: {outcome}")
        logger.info(f"{'='*50}\n")
        self._healing = False


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    orch = DemoOrchestrator()
    try:
        await orch.start()
    except KeyboardInterrupt:
        logger.info("Orchestrator stopped.")
    except Exception as exc:
        logger.error(f"Orchestrator error: {exc}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
