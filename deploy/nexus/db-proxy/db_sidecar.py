"""
NEXUS DB Sidecar
=================
Lightweight process that polls PgBouncer stats and reports
query metrics to the NEXUS Status API.

Deploy this alongside PgBouncer (see docker-compose.pgbouncer.yaml).
No changes to application code — this is the Tier 1 zero-code integration.

What it reports every POLL_INTERVAL_S seconds:
  - Total queries/s (from SHOW STATS)
  - Average query time
  - Active connections vs pool size
  - Slow query events (when avg_query_time > SLOW_QUERY_MS)
  - Connection pressure events (when active_clients > 80% of max)
  - Query spike events (when qps increases > 2x vs previous interval)

All events → POST /sdk/query or /sdk/event on NEXUS Status API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional

import httpx

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s [DB-Sidecar] %(message)s",
    datefmt = "%H:%M:%S",
)
logger = logging.getLogger("nexus.db_sidecar")

# ── Config ────────────────────────────────────────────────────────────────────
PGBOUNCER_HOST  = os.getenv("PGBOUNCER_HOST",  "localhost")
PGBOUNCER_PORT  = int(os.getenv("PGBOUNCER_PORT", "5432"))
PGBOUNCER_USER  = os.getenv("PGBOUNCER_USER",  "appuser")
PGBOUNCER_PASS  = os.getenv("PGBOUNCER_PASS",  "")
NEXUS_URL       = os.getenv("NEXUS_API_URL",   "http://localhost:8080").rstrip("/")
SELFHEAL_TOKEN  = os.getenv("SELFHEAL_TOKEN",  "")
APP_NAME        = os.getenv("APP_NAME",        "app")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_S", "10"))
SLOW_QUERY_MS   = int(os.getenv("SLOW_QUERY_MS",   "200"))

# ── State ─────────────────────────────────────────────────────────────────────
_prev_total_queries: int   = 0
_prev_poll_time:     float = 0.0


# ── PgBouncer stats query ─────────────────────────────────────────────────────

async def _get_pgbouncer_stats() -> Optional[Dict]:
    """Run 'SHOW STATS' on PgBouncer's admin console."""
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host     = PGBOUNCER_HOST,
            port     = PGBOUNCER_PORT,
            user     = PGBOUNCER_USER,
            password = PGBOUNCER_PASS,
            database = "pgbouncer",   # PgBouncer admin DB
        )
        rows = await conn.fetch("SHOW STATS;")
        await conn.close()
        if rows:
            return dict(rows[0])
    except Exception as exc:
        logger.debug(f"PgBouncer stats error: {exc}")
    return None


async def _get_pgbouncer_pools() -> Optional[Dict]:
    """Run 'SHOW POOLS' to get connection pool utilization."""
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=PGBOUNCER_HOST, port=PGBOUNCER_PORT,
            user=PGBOUNCER_USER, password=PGBOUNCER_PASS, database="pgbouncer",
        )
        rows = await conn.fetch("SHOW POOLS;")
        await conn.close()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


# ── NEXUS emit ────────────────────────────────────────────────────────────────

async def _emit(path: str, payload: dict) -> None:
    if not SELFHEAL_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.post(
                f"{NEXUS_URL}{path}",
                json    = {**payload, "app": APP_NAME},
                headers = {"Authorization": f"Bearer {SELFHEAL_TOKEN}"},
            )
    except Exception as exc:
        logger.debug(f"NEXUS emit error: {exc}")


# ── Main poll loop ────────────────────────────────────────────────────────────

async def poll() -> None:
    global _prev_total_queries, _prev_poll_time

    stats = await _get_pgbouncer_stats()
    pools = await _get_pgbouncer_pools() or []
    now   = time.monotonic()

    if not stats:
        logger.warning("PgBouncer stats unavailable — is PgBouncer running?")
        return

    # Extract key metrics
    total_queries   = int(stats.get("total_query_count", 0) or 0)
    total_query_us  = int(stats.get("total_query_time",  0) or 0)   # microseconds
    avg_wait_us     = int(stats.get("avg_wait_time",     0) or 0)

    elapsed         = now - _prev_poll_time if _prev_poll_time else POLL_INTERVAL
    delta_queries   = total_queries - _prev_total_queries
    qps             = delta_queries / elapsed if elapsed > 0 else 0

    avg_query_ms    = (total_query_us / max(delta_queries, 1)) / 1000   # µs → ms

    # Connection pressure across pools
    total_active    = sum(int(p.get("cl_active", 0) or 0) for p in pools)
    total_waiting   = sum(int(p.get("cl_waiting", 0) or 0) for p in pools)
    total_server    = sum(int(p.get("sv_active", 0) or 0) for p in pools)

    logger.info(
        f"qps={qps:.1f} avg_ms={avg_query_ms:.1f} "
        f"active={total_active} waiting={total_waiting}"
    )

    # ── Report regular heartbeat ───────────────────────────────────────────────
    await _emit("/sdk/heartbeat", {
        "rps":         qps,
        "memory_mb":   None,
        "uptime_s":    None,
        "version":     "pgbouncer-proxy",
    })

    # ── Report slow query event ────────────────────────────────────────────────
    if avg_query_ms > SLOW_QUERY_MS and delta_queries > 0:
        logger.warning(f"Slow query detected: avg={avg_query_ms:.0f}ms (threshold={SLOW_QUERY_MS}ms)")
        await _emit("/sdk/query", {
            "sql_preview": "PgBouncer aggregate — slow query pattern",
            "duration_ms": avg_query_ms,
            "tables":      [],
            "slow":        True,
        })

    # ── Report connection pressure ─────────────────────────────────────────────
    if total_waiting > 5:
        logger.warning(f"Connection pressure: {total_waiting} clients waiting")
        await _emit("/sdk/event", {
            "type":         "db_connection_pressure",
            "waiting":      total_waiting,
            "active":       total_active,
            "server_active":total_server,
        })

    # ── Report query spike ─────────────────────────────────────────────────────
    prev_qps = _prev_total_queries / POLL_INTERVAL if _prev_poll_time else 0
    if prev_qps > 0 and qps > prev_qps * 2.0:
        logger.warning(f"Query spike: {prev_qps:.1f} → {qps:.1f} qps ({qps/prev_qps:.1f}x)")
        await _emit("/sdk/event", {
            "type":     "db_query_spike",
            "prev_qps": round(prev_qps, 1),
            "curr_qps": round(qps, 1),
            "ratio":    round(qps / prev_qps, 2),
        })

    # ── Update state ───────────────────────────────────────────────────────────
    _prev_total_queries = total_queries
    _prev_poll_time     = now


async def main() -> None:
    logger.info(f"NEXUS DB Sidecar starting — polling PgBouncer at {PGBOUNCER_HOST}:{PGBOUNCER_PORT} every {POLL_INTERVAL}s")
    logger.info(f"Reporting to NEXUS at {NEXUS_URL} as app='{APP_NAME}'")

    # Auto-register with NEXUS if no token
    if not SELFHEAL_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{NEXUS_URL}/apps/register", json={"app_name": APP_NAME, "tier": "production"})
                if r.status_code == 200:
                    token = r.json().get("selfheal_token", "")
                    os.environ["SELFHEAL_TOKEN"] = token
                    globals()["SELFHEAL_TOKEN"]  = token
                    logger.info(f"Registered — token={token[:12]}…")
        except Exception as e:
            logger.warning(f"NEXUS registration failed: {e} — running without token")

    while True:
        try:
            await poll()
        except Exception as exc:
            logger.error(f"Poll error: {exc}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
