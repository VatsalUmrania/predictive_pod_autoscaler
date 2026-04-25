"""
ShopDemo Backend — NEXUS Self-Healing Integration Demo
========================================================
A simple e-commerce API that integrates the NEXUS SDK exactly as a real
developer would after running:
    pip install nexus-selfheal   (or in this demo: pip install -e ../../.[nexus])

What this shows:
  1. SDK auto-registration (SELFHEAL_TOKEN issued on first startup)
  2. ASGI middleware capturing every HTTP 5xx automatically
  3. @SelfHeal.query() decorator on DB queries (feeds DBTrafficCorrelator)
  4. Manual failure injection via /inject/* endpoints (for demo buttons)
  5. Git push simulation via /simulate/push (runs GitAgent validation logic)
"""

from __future__ import annotations

import asyncio
import gc
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── NEXUS SDK (installed via: pip install -e ../../.[nexus]) ──────────────────
from nexus.sdk.python import SelfHeal
from nexus.sdk.python.middleware import SelfHealMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH        = os.getenv("DEMO_DB_PATH", "shop.db")
NEXUS_URL      = os.getenv("NEXUS_API_URL", "http://localhost:8080")
APP_NAME       = os.getenv("APP_NAME", "shop-demo")
_TOKEN         = os.getenv("SELFHEAL_TOKEN", "")

# ── Failure injection state (in-memory flags) ─────────────────────────────────
_failures: Dict[str, Any] = {
    "error_until":   0.0,       # epoch — return 500 until this time
    "slow_query":    False,     # sleep 3s in every DB query
    "dns_error":     False,     # return DNS failure on /products
    "missing_env":   False,     # report ENV_CONTRACT_VIOLATION
    "memory_hog":    [],        # list of large byte arrays (simulate leak)
    "bad_config":    False,     # return config error flag
}

# ── DB helpers ────────────────────────────────────────────────────────────────

SEED_PRODUCTS = [
    (1, "MacBook Pro M3",  2499.00, 50,  "Laptops",     "💻"),
    (2, "AirPods Pro 2",    249.00, 200, "Audio",       "🎧"),
    (3, "iPhone 15 Pro",   1299.00, 150, "Phones",      "📱"),
    (4, "iPad Air M2",      749.00, 80,  "Tablets",     "📟"),
    (5, "Apple Watch S9",   399.00, 120, "Wearables",   "⌚"),
    (6, "HomePod Mini",      99.00, 60,  "Smart Home",  "🔊"),
    (7, "USB-C Hub Pro",     79.00, 300, "Accessories", "🔌"),
    (8, "MagSafe Wallet",    59.00, 250, "Accessories", "💳"),
]


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id       INTEGER PRIMARY KEY,
                name     TEXT    NOT NULL,
                price    REAL    NOT NULL,
                stock    INTEGER DEFAULT 100,
                category TEXT    DEFAULT 'General',
                emoji    TEXT    DEFAULT '📦'
            );
            CREATE TABLE IF NOT EXISTS orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id   INTEGER NOT NULL,
                product_name TEXT,
                quantity     INTEGER DEFAULT 1,
                total        REAL    NOT NULL,
                status       TEXT    DEFAULT 'completed',
                created_at   TEXT    DEFAULT CURRENT_TIMESTAMP
            );
        """)
        async with db.execute("SELECT COUNT(*) FROM products") as cur:
            if (await cur.fetchone())[0] == 0:
                await db.executemany(
                    "INSERT OR IGNORE INTO products (id,name,price,stock,category,emoji) VALUES (?,?,?,?,?,?)",
                    SEED_PRODUCTS,
                )
        await db.commit()


# ── NEXUS SDK helpers (direct HTTP — shows exactly what the middleware does) ──

async def _nexus_post(path: str, payload: dict) -> None:
    """Fire-and-forget POST to NEXUS Status API."""
    token = _TOKEN or os.environ.get("SELFHEAL_TOKEN", "")
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(
                f"{NEXUS_URL}{path}",
                json={**payload, "app": APP_NAME},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass


async def _emit_query(label: str, duration_ms: float, tables: List[str]) -> None:
    await _nexus_post("/sdk/query", {
        "sql_preview": label,
        "duration_ms": duration_ms,
        "tables":      tables,
        "slow":        duration_ms > 200,
    })


async def _emit_route_error(route: str, status: int, msg: str = "") -> None:
    await _nexus_post("/sdk/route-error", {
        "route":       route,
        "method":      "GET",
        "status_code": status,
        "error_msg":   msg,
    })


async def _emit_event(event_type: str, data: dict) -> None:
    await _nexus_post("/sdk/event", {"type": event_type, **data})


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    global _TOKEN
    await init_db()

    # Auto-register with NEXUS (gets SELFHEAL_TOKEN if not set)
    if not _TOKEN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"{NEXUS_URL}/apps/register",
                    json={"app_name": APP_NAME, "tier": "production"},
                )
                if r.status_code == 200:
                    _TOKEN = r.json().get("selfheal_token", "")
                    os.environ["SELFHEAL_TOKEN"] = _TOKEN
                    print(f"\n[ShopDemo] ✅ Registered with NEXUS")
                    print(f"[ShopDemo] 🔑 SELFHEAL_TOKEN = {_TOKEN[:16]}…\n")
        except Exception as e:
            print(f"[ShopDemo] ⚠️  NEXUS registration failed ({e}) — running without SDK")

    # Load selfheal.yaml and cache policy in NEXUS dashboard
    try:
        from nexus.integration.selfheal_config import load_selfheal_config
        from nexus.integration.dashboard import cache_policy
        cfg = load_selfheal_config(Path(__file__).parent)
        if cfg:
            cache_policy(cfg.app, cfg.to_dict())
            print(f"[ShopDemo] 📋 selfheal.yaml loaded for app='{cfg.app}'")
    except Exception:
        pass

    yield
    print("[ShopDemo] Shutting down.")


# ── FastAPI app ───────────────────────────────────────────────────────────────

_inner = FastAPI(title="ShopDemo", version="1.0.0", lifespan=lifespan)

_inner.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# NEXUS SDK middleware — wraps every request (installed via pip install -e ../../.[nexus])
# Automatically captures 5xx errors and slow responses → sends to NEXUS
_inner.add_middleware(
    SelfHealMiddleware,
    token     = _TOKEN or os.environ.get("SELFHEAL_TOKEN", ""),
    nexus_url = NEXUS_URL,
    slow_ms   = 500,
)

app = _inner   # uvicorn target


# ── Shop endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "app": APP_NAME, "failures": {
        k: v for k, v in _failures.items() if k != "memory_hog"
    }}


@app.get("/nexus/token")
def nexus_token() -> Dict[str, str]:
    t = _TOKEN or os.environ.get("SELFHEAL_TOKEN", "")
    return {"token": t, "prefix": (t[:12] + "…") if t else "not registered"}


@app.get("/products")
async def get_products() -> List[Dict]:
    # Check active failures
    if _failures["dns_error"]:
        await _emit_route_error("/products", 503, "DNS resolution failure — upstream unreachable")
        raise HTTPException(503, "DNS resolution failure (injected)")

    if time.time() < _failures["error_until"]:
        await _emit_route_error("/products", 500, "Internal server error — bad deploy simulation")
        raise HTTPException(500, "Service unavailable (injected error)")

    start = time.monotonic()
    if _failures["slow_query"]:
        await asyncio.sleep(3.0)   # simulate 3s slow query

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products ORDER BY id") as cur:
            rows = await cur.fetchall()

    duration_ms = (time.monotonic() - start) * 1000
    asyncio.create_task(_emit_query("SELECT * FROM products", duration_ms, ["products"]))
    return [dict(r) for r in rows]


@app.get("/orders")
async def get_orders() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 30"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


class OrderReq(BaseModel):
    product_id: int
    quantity:   int = 1


@app.post("/orders")
async def place_order(req: OrderReq) -> Dict:
    if time.time() < _failures["error_until"]:
        await _emit_route_error("/orders", 500, "Checkout unavailable — injected error")
        raise HTTPException(500, "Checkout unavailable (injected error)")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM products WHERE id=?", (req.product_id,)) as cur:
            product = await cur.fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        p     = dict(product)
        total = p["price"] * req.quantity
        await db.execute(
            "INSERT INTO orders (product_id, product_name, quantity, total) VALUES (?,?,?,?)",
            (req.product_id, p["name"], req.quantity, total),
        )
        await db.commit()

    asyncio.create_task(
        _emit_query(f"INSERT INTO orders (product_id={req.product_id})",
                    random.uniform(8, 30), ["orders", "products"])
    )
    return {"status": "ok", "product": p["name"], "total": total, "quantity": req.quantity}


# ── Failure injection ─────────────────────────────────────────────────────────

@app.post("/inject/db-spike")
async def inject_db_spike(bg: BackgroundTasks) -> Dict:
    """Fire 300 rapid DB queries — triggers DBTrafficCorrelator spike detection."""
    async def _spike():
        async with aiosqlite.connect(DB_PATH) as db:
            for _ in range(300):
                async with db.execute("SELECT * FROM orders ORDER BY RANDOM() LIMIT 10") as cur:
                    await cur.fetchall()
                asyncio.create_task(
                    _emit_query("SELECT * FROM orders", random.uniform(5, 40), ["orders"])
                )
                await asyncio.sleep(0.01)
    bg.add_task(_spike)
    await _emit_event("db_spike_injected", {"count": 300, "table": "orders"})
    return {"injected": "db_spike", "queries": 300, "message": "Firing 300 ORDER table queries over 3 seconds"}


@app.post("/inject/error-rate")
async def inject_error_rate() -> Dict:
    """Return HTTP 500 on all routes for 30 seconds."""
    _failures["error_until"] = time.time() + 30
    await _emit_event("error_rate_injected", {"duration_seconds": 30, "routes": ["*"]})
    return {"injected": "error_rate", "duration_seconds": 30, "message": "All routes will return 500 for 30s"}


@app.post("/inject/slow-query")
async def inject_slow_query() -> Dict:
    """Add 3-second sleep to every DB query."""
    _failures["slow_query"] = not _failures["slow_query"]
    state = "enabled" if _failures["slow_query"] else "disabled"
    await _emit_event("slow_query_injected", {"active": _failures["slow_query"], "delay_ms": 3000})
    return {"injected": "slow_query", "state": state, "delay_ms": 3000 if _failures["slow_query"] else 0}


@app.post("/inject/oom")
async def inject_oom() -> Dict:
    """Allocate 200MB — simulates OOM / memory pressure."""
    block = bytearray(200 * 1024 * 1024)   # 200 MB
    _failures["memory_hog"].append(block)
    await _emit_event("oom_injected", {"allocated_mb": 200, "total_blocks": len(_failures["memory_hog"])})
    return {"injected": "oom", "allocated_mb": 200,
            "total_allocated_mb": len(_failures["memory_hog"]) * 200,
            "message": "200MB allocated — simulating memory pressure"}


@app.post("/inject/missing-env")
async def inject_missing_env() -> Dict:
    """Report a missing-env violation to NEXUS (simulates git push with bad env config)."""
    missing = ["STRIPE_SECRET_KEY", "PAYMENT_GATEWAY_URL", "DATABASE_ENCRYPTION_KEY"]
    await _nexus_post("/sdk/event", {
        "type":         "env_contract_violation",
        "missing_keys": missing,
        "source":       "git-push-hook",
        "branch":       "main",
        "sha":          "a3f2c8b",
        "deployment":   "shop-demo",
    })
    return {"injected": "missing_env", "missing_keys": missing,
            "message": "Reported ENV_CONTRACT_VIOLATION to NEXUS"}


@app.post("/inject/dns-error")
async def inject_dns_error() -> Dict:
    """Toggle DNS error flag — next /products call fails with 503."""
    _failures["dns_error"] = not _failures["dns_error"]
    state = "enabled" if _failures["dns_error"] else "disabled"
    if _failures["dns_error"]:
        await _emit_event("dns_error_injected", {"host": "payment-service.internal", "active": True})
    return {"injected": "dns_error", "state": state,
            "message": f"/products will return 503 DNS failure: {state}"}


@app.post("/inject/bad-config")
async def inject_bad_config() -> Dict:
    """Write invalid JSON to a config and report CONFIG_DRIFT to NEXUS."""
    _failures["bad_config"] = True
    await _emit_event("config_drift_detected", {
        "file":    "config/database.json",
        "reason":  "Invalid JSON — missing closing bracket",
        "line":    47,
        "diff":    '+  "max_connections": "unlimited"  # ← string instead of int',
    })
    return {"injected": "bad_config", "file": "config/database.json",
            "message": "Config drift reported to NEXUS"}


@app.post("/inject/heal-all")
async def inject_heal_all() -> Dict:
    """Reset all active failures (called by demo orchestrator after healing)."""
    _failures["error_until"] = 0.0
    _failures["slow_query"]  = False
    _failures["dns_error"]   = False
    _failures["bad_config"]  = False
    _failures["missing_env"] = False
    freed = len(_failures["memory_hog"])
    _failures["memory_hog"].clear()
    gc.collect()
    return {"healed": True, "freed_memory_blocks": freed,
            "message": "All injected failures reset — system restored"}


# ── Git push simulation ───────────────────────────────────────────────────────

_SCENARIOS: Dict[str, Dict[str, str]] = {
    "missing-env": {
        "filename": "checkout.py",
        "content":  (
            "import os\n"
            "from database import get_connection\n\n"
            "STRIPE_SECRET = os.environ['STRIPE_SECRET_KEY']   # required\n"
            "PAYMENT_URL   = os.getenv('PAYMENT_GATEWAY_URL')  # required\n"
            "DB_ENCRYPT_KEY = os.environ['DATABASE_ENCRYPTION_KEY']  # required\n\n"
            "def process_payment(amount, card_token):\n"
            "    # Call Stripe API\n"
            "    headers = {'Authorization': f'Bearer {STRIPE_SECRET}'}\n"
            "    ...\n"
        ),
    },
    "leaked-secret": {
        "filename": "config.py",
        "content": (
            "# App configuration\n"
            "DATABASE_URL = 'postgresql://admin:password@localhost/shopdb'\n"
            "STRIPE_KEY = 'sk-live-abc123def456ghi789jkl012mno345pqr678'\n"
            "AWS_ACCESS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"
            "AWS_SECRET = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n"
            "GITHUB_TOKEN = 'ghp_16C7e42F292c6912E7710c838347Ae5B21'\n"
            "DEBUG = True\n"
        ),
    },
    "bad-code": {
        "filename": "orders.py",
        "content": (
            "from database import db\n\n"
            "def calculate_discount(price, discount_pct):\n"
            "    # Bug: ZeroDivisionError when discount_pct == 100\n"
            "    multiplier = 1 / (100 - discount_pct)  # ← division by zero\n"
            "    return price * multiplier\n\n"
            "def get_order_total(order_id):\n"
            "    order = db.query(f'SELECT * FROM orders WHERE id={order_id}')  # ← SQL injection\n"
            "    return calculate_discount(order.price, order.discount)\n"
        ),
    },
    "clean": {
        "filename": "utils.py",
        "content": (
            "import os\nfrom typing import Optional\n\n"
            "LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')\n"
            "APP_VERSION = '1.2.3'\n\n"
            "def format_price(amount: float) -> str:\n"
            "    return f'${amount:.2f}'\n"
        ),
    },
}

_SECRET_PATTERNS = [
    (re.compile(r'(?i)(api_key|apikey|api-key)\s*[=:]\s*["\'][a-zA-Z0-9_\-]{20,}["\']'), "api_key"),
    (re.compile(r'(?i)(secret|password|passwd)\s*[=:]\s*["\'][^"\']{8,}["\']'), "password"),
    (re.compile(r'AKIA[0-9A-Z]{16}'), "aws_access_key"),
    (re.compile(r'sk-[a-zA-Z0-9\-]{30,}'), "stripe_key"),
    (re.compile(r'ghp_[a-zA-Z0-9]{36}'), "github_token"),
    (re.compile(r'(?i)postgres://[^:]+:[^@]{6,}@'), "db_connection_string"),
]

_ENV_KEYS_RE = re.compile(r"os\.(?:environ\[|getenv\()['\"]([A-Z_][A-Z0-9_]*)")
_KNOWN_ENV   = {"PATH","HOME","USER","LOG_LEVEL","APP_VERSION","DEBUG","PORT","HOST","NODE_ENV"}


@app.post("/simulate/push")
async def simulate_push(scenario: str = "missing-env") -> Dict:
    """
    Simulate a git push with a specific scenario.
    Runs the same validation logic as GitAgent (env extraction + secret scan).
    Returns findings with file names and line numbers — exactly what GitAgent would report.
    """
    if scenario not in _SCENARIOS:
        raise HTTPException(400, f"Unknown scenario. Choose: {list(_SCENARIOS.keys())}")

    s        = _SCENARIOS[scenario]
    filename = s["filename"]
    content  = s["content"]

    findings: List[Dict] = []

    # 1. Scan for leaked secrets (same as GitAgent.scan_diff_for_secrets)
    secret_findings = []
    for i, line in enumerate(content.splitlines(), 1):
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(line):
                secret_findings.append({"line": i, "label": label,
                                         "snippet": line.strip()[:80]})

    # 2. Extract required env keys (same as GitAgent.extract_env_keys_python)
    required_keys = set(_ENV_KEYS_RE.findall(content)) - _KNOWN_ENV
    missing_keys  = [k for k in required_keys if not os.environ.get(k)]

    # 3. Detect obvious code bugs (simple regex heuristics for demo)
    code_issues = []
    for i, line in enumerate(content.splitlines(), 1):
        if "1 / (100 -" in line or "/ 0" in line:
            code_issues.append({"line": i, "issue": "potential_division_by_zero",
                                 "snippet": line.strip()})
        if "f'" in line and "SELECT" in line and "{" in line:
            code_issues.append({"line": i, "issue": "sql_injection_risk",
                                 "snippet": line.strip()[:80]})

    # 4. Build result
    passed  = not secret_findings and not missing_keys and not code_issues
    signals = []

    if secret_findings:
        signals.append({"signal": "SECRET_COMMITTED",   "severity": "CRITICAL", "findings": secret_findings})
        asyncio.create_task(_emit_event("secret_committed", {
            "sha": "b4d1e2f", "branch": "main", "file": filename,
            "findings": secret_findings[:5],
        }))

    if missing_keys:
        signals.append({"signal": "ENV_CONTRACT_VIOLATION", "severity": "CRITICAL",
                         "missing_keys": list(missing_keys)})
        asyncio.create_task(_emit_event("env_contract_violation", {
            "sha": "b4d1e2f", "branch": "main", "missing_keys": list(missing_keys),
            "deployment": APP_NAME,
        }))

    if code_issues:
        signals.append({"signal": "CODE_QUALITY_WARNING", "severity": "WARNING",
                         "issues": code_issues})
        asyncio.create_task(_emit_event("code_quality_warning", {
            "sha": "b4d1e2f", "branch": "main", "file": filename, "issues": code_issues,
        }))

    if passed:
        signals.append({"signal": "DEPLOY_EVENT", "severity": "INFO",
                         "message": "All checks passed — deploy proceeding"})
        asyncio.create_task(_emit_event("deploy_event", {
            "sha": "c1e2f3a", "branch": "main", "author": "dev@shopdemo.com",
            "message": "Clean push — no issues found",
        }))

    return {
        "scenario":      scenario,
        "file_analyzed": filename,
        "passed":        passed,
        "signals":       signals,
        "summary": (
            f"{'✅ All checks passed' if passed else '❌ Issues found'}: "
            f"{len(secret_findings)} secrets, {len(missing_keys)} missing env keys, "
            f"{len(code_issues)} code issues"
        ),
    }


# ── NEXUS proxy (convenience — avoids CORS for console.html polling) ──────────

@app.get("/nexus/incidents")
async def proxy_incidents(n: int = 20, app: Optional[str] = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            url = f"{NEXUS_URL}/developer/incidents?n={n}"
            if app:
                url += f"&app={app}"
            r = await client.get(url)
            return r.json()
    except Exception:
        return []


@app.get("/nexus/status")
async def proxy_status() -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{NEXUS_URL}/status")
            return r.json()
    except Exception:
        return {"error": "NEXUS Status API unreachable"}


@app.get("/nexus/apps")
async def proxy_apps() -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{NEXUS_URL}/apps")
            return r.json()
    except Exception:
        return []


@app.get("/nexus/audit")
async def proxy_audit(n: int = 10) -> Any:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{NEXUS_URL}/audit/tail?n={n}")
            return r.json()
    except Exception:
        return []
