# NEXUS Demo — ShopDemo Application

A complete demo that shows NEXUS self-healing integrated into a real e-commerce app.

## Quick Start (5 minutes)

```bash
# 1. From the repo root — start everything
cd demo
cp backend/.env.example backend/.env

# 2. Run locally (no Docker needed for dev)
cd backend
pip install -r requirements.txt   # installs nexus SDK via pip install -e ../../.[nexus]
uvicorn main:app --port 8001 --reload &
python demo_orchestrator.py &      # starts the reasoning loop

# 3. Open the NEXUS Status API (must be running separately)
# See deploy/nexus/docker-compose.dev.yaml

# 4. Open in browser
open frontend/shop.html      # E-commerce store
open frontend/console.html   # NEXUS Console
```

## Full Docker Stack

```bash
cd demo
docker compose up -d
```

| Service | URL | What it is |
|---------|-----|-----------|
| ShopDemo | http://localhost:8001 | Demo e-commerce backend |
| NEXUS API | http://localhost:8080 | Status API + SDK ingest |
| Grafana | http://localhost:3000 | Metrics dashboard (admin/nexus_admin) |
| Prometheus | http://localhost:9090 | Metrics |
| NATS | nats://localhost:4222 | Event bus |

## Presentation Script

### 1. Show the shop (shop.html)
- Open shop.html — premium dark e-commerce store
- Add items to cart, place an order — shows real SQLite writes
- NEXUS badge in header shows "NEXUS ACTIVE"

### 2. Open the NEXUS Console (console.html)
- Show the live event feed (empty at first)
- Point to the 6 agent status cards
- Show the Failure Lab buttons

### 3. Trigger failures and watch NEXUS respond

| Button | What you say | What NEXUS does |
|--------|-------------|-----------------|
| **DB Query Spike** | "Simulating a traffic spike on the orders table" | 300 queries fire → feed shows DB_QUERY events → Orchestrator detects spike → heals |
| **Error Rate** | "Simulating a bad deploy — all routes start failing" | All routes → 500 for 30s → feed shows ERR_RATE → Orchestrator rolls back → heals |
| **Slow Query** | "Database is responding slowly" | 3s sleep on every query → SLOW_QRY event → Orchestrator alerts |
| **Missing ENV** | "Developer pushed code with missing environment variables" | ENV_FAIL event → Orchestrator blocks deploy |
| **DNS Error** | "Upstream service DNS is failing" | Shop /products returns 503 → DNS_ERR → Orchestrator flushes DNS |
| **Heal All** | "Manually reset for the next demo" | All failures reset, all agents go green |

### 4. Git Push Simulation
- Click **Push: Leaked Secret** — shows GitAgent finding AWS key + Stripe key with line numbers
- Click **Push: Missing ENV** — shows ENV_CONTRACT_VIOLATION with exact missing keys
- Click **Push: Bad Code** — shows division by zero + SQL injection risk
- Click **Push: Clean** — shows all checks passing, deploy proceeds

### 5. Show the audit trail
- Open http://localhost:8080/developer/incidents in browser
- Every healing action has a plain-English description
- Open http://localhost:8080/docs — shows all 30+ API endpoints

### 6. Show Grafana (optional)
- Open http://localhost:3000 — 13-panel dashboard
- NEXUS metrics: success rate, false-heal rate, RCA confidence

## How SDK Integration Works

The demo backend installs NEXUS exactly like a real developer:

```bash
pip install -e ../../.[nexus]   # editable install — in prod: pip install nexus-selfheal
```

Then in `backend/main.py`:
```python
from nexus.sdk.python import SelfHeal
from nexus.sdk.python.middleware import SelfHealMiddleware

# Auto-registers app on startup, gets SELFHEAL_TOKEN
app.add_middleware(SelfHealMiddleware, token=TOKEN, nexus_url=NEXUS_URL)
```

That's it. The middleware captures every 5xx automatically.

For deeper integration:
```python
@SelfHeal.query(label="orders", spike_indicator=True)
def get_orders():
    ...

@SelfHeal.critical(never_shed=True, fallback="queue")
async def process_payment():
    ...
```

## Reasoning Loop

The `demo_orchestrator.py` runs the full sense→reason→act→verify loop:

```
SDK event arrives → NATS → Demo Orchestrator
                              │
                    Correlate (30s window)
                              │
                    Rule-based RCA (+ optional Gemini)
                              │
                    Select runbook + confidence score
                              │
                    Execute healing (reset demo failures)
                              │
                    Write to AuditTrail
                              │
                    Publish result → Console live feed
```
