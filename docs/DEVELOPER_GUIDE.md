# Development Guide

## Quick Start

### Setup

```bash
git clone <repo>
cd ppa
make install-dev
```

### Running Tests

```bash
# Unit tests only (fastest, ~1 minute)
make test

# By category
make test-unit          # Fast unit tests
make test-integration   # Component interaction tests
make test-e2e           # Full system tests (slow, ~5 mins)

# All tests
pytest tests/
```

### Code Quality

```bash
# Check for issues
make lint

# Auto-fix code style
make format

# Strict type checking
make typecheck
```

## Project Structure

```
src/ppa/                    # All application code (canonical import root)
├── __init__.py            # Version, public API
├── config.py              # ✅ Centralized configuration (single source of truth)
├── common/                # Shared utilities
│   ├── constants.py
│   ├── feature_spec.py
│   └── promql.py
├── operator/              # Kubernetes operator
│   ├── main.py
│   ├── features.py
│   ├── predictor.py
│   └── scaler.py
├── model/                 # ML training & inference
│   ├── train.py
│   ├── evaluate.py
│   ├── pipeline.py
│   └── convert.py
├── dataflow/              # Metrics collection
│   ├── export_training_data.py
│   ├── validate_training_data.py
│   └── verify_features.py
└── cli/                   # Command-line interface
    └── commands/

tests/                      # Test suite (organized by type)
├── conftest.py            # Shared fixtures
├── fixtures/              # Test data & mocks
├── unit/                  # Fast unit tests
├── integration/           # Component tests
└── e2e/                   # Full system tests

data/                       # Runtime data (NOT in VCS — created at runtime)
├── training-data/         # Prometheus-exported CSVs for model training
├── artifacts/             # Trained .keras models, scalers, TFLite models
├── champions/             # Production-approved model sets
└── test-app/             # Instrumented test application source

deploy/                     # Kubernetes manifests
docs/                       # Documentation (this directory)
```

## Configuration

All configuration is centralized in `src/ppa/config.py`:

```python
from ppa.config import get_config

config = get_config()
print(config.prometheus.url)
print(config.operator.namespace)
```

### Environment Variables

```bash
# Prometheus
PROMETHEUS_URL=http://localhost:9090
PROM_TIMEOUT=2
PPA_PROM_FAILURE_THRESHOLD=10

# Operator
PPA_NAMESPACE=default
PPA_TIMER_INTERVAL=30
PPA_INITIAL_DELAY=60
PPA_STABILIZATION_STEPS=2
PPA_STABILIZATION_TOLERANCE=0.5
PPA_LOOKBACK_STEPS=60
LOG_LEVEL=INFO

# Model
PPA_MODEL_DIR=/models
PPA_DEFAULT_HORIZON=rps_t10m

# Scaling
PPA_MIN_REPLICAS=2
PPA_MAX_REPLICAS=20
PPA_SCALE_UP_RATE=2.0
PPA_SCALE_DOWN_RATE=0.5
PPA_CAPACITY_PER_POD=50
PPA_SAFETY_FACTOR=1.10

# Data Collection
TARGET_APP=test-app
NAMESPACE=default
CONTAINER_NAME=test-app

# CLI
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
APP_PORT=8080
```

## Common Workflows

### Add a New Test

1. Create test in appropriate directory:
   - `tests/unit/test_*.py` for unit tests
   - `tests/integration/test_*.py` for component tests
   - `tests/e2e/test_*.py` for full system tests

2. Imports:
   ```python
   from ppa.config import get_config, set_config
   from ppa.operator.features import extract_features
   # etc.
   ```

3. Use fixtures from `conftest.py`:
   ```python
   def test_something(test_config, mock_prometheus_response):
       set_config(test_config)
       # ...
   ```

### Fix a Failing Test

1. Run the specific test:
   ```bash
   pytest tests/unit/test_foo.py::TestClass::test_method -v
   ```

2. Check imports are correct (should be `from ppa.*`)

3. Use `set_config()` in tests to inject test configs

### Add a New Feature

1. Create code in appropriate `src/ppa/*/` module
2. Add tests in `tests/unit/` and `tests/integration/`
3. Update docstrings and type hints
4. Run `make lint format test`
5. Update relevant docs in `docs/`

## Debugging

### Enable debug logging

```bash
LOG_LEVEL=DEBUG ppa <command>
```

### Run Python REPL with config

```bash
python -c "from ppa.config import get_config; c = get_config(); print(c)"
```

### Check imports work

```bash
python -c "from ppa.operator.features import extract_features; print('OK')"
```

## Troubleshooting

**Import errors like `No module named 'ppa.X'`?**
- Run `make install-dev` to install the package in editable mode
- Ensure your shell/editor uses the right Python interpreter

**Tests fail with config not found?**
- Ensure `src/ppa/config.py` exists and has the right structure
- Tests should use the `test_config` fixture from `conftest.py`

**Linting errors?**
- Run `make format` to auto-fix most issues
- `make lint` will show remaining issues

## Useful Commands

```bash
# Run one test file
pytest tests/unit/test_foo.py -v

# Run tests matching a pattern
pytest -k "test_feature" -v

# Run with coverage
pytest --cov=src/ppa --cov-report=html

# Run with extra verbosity
pytest -vv --tb=long
```
# NEXUS Self-Healing — Developer Guide

---

## Part 1: Integrating NEXUS Into Your App

### Step 1 — Install the SDK

**Python (FastAPI / Django / Flask)**
```bash
# From GitHub (current — until published to PyPI)
pip install git+https://github.com/VatsalUmrania/predictive_pod_autoscaler.git#egg=nexus-selfheal

# Future: once published to PyPI
pip install nexus-selfheal
```

**Node.js (Express / Fastify)**
```bash
# Copy sdk file from the repo
# Future: npm install @nexus/selfheal
```

**Frontend**
```html
<script src="https://your-nexus-server.com/sdk/beacon.js"
        data-app="my-app"
        data-token="pub_xxxx"></script>
```

---

### Step 2 — Get a Token

Two ways to get your `SELFHEAL_TOKEN`:

**Option A — Auto (recommended):** Commit a `selfheal.yaml` to your repo root. NEXUS issues a token automatically when you push to GitHub (via the Git webhook).

**Option B — Manual:**
```bash
curl -X POST https://your-nexus-server.com/apps/register \
  -H "Content-Type: application/json" \
  -d '{"app_name": "my-app", "tier": "production"}'

# Response: { "selfheal_token": "sh_abc123..." }
```

Store it as an environment variable:
```bash
export SELFHEAL_TOKEN=sh_abc123...
export NEXUS_API_URL=https://your-nexus-server.com
```

---

### Step 3 — Add the SDK (2 lines)

**Python (FastAPI)**
```python
import os
from selfheal import SelfHeal

app = FastAPI()
SelfHeal.init(app, token=os.environ["SELFHEAL_TOKEN"])
# Done. Middleware is now active — all 5xx errors are captured automatically.
```

**Python (Django/Flask — WSGI)**
```python
SelfHeal.init(app, token=os.environ["SELFHEAL_TOKEN"], wsgi=True)
```

**Node.js (Express)**
```javascript
const { SelfHeal } = require('./selfheal')

SelfHeal.init({
  appName: 'my-app',
  token:   process.env.SELFHEAL_TOKEN,
  criticalRoutes: ['/api/checkout', '/api/payment']
})

app.use(SelfHeal.middleware())
```

---

### Step 4 — Add `selfheal.yaml` to Your Repo Root

```yaml
# selfheal.yaml
app: my-app
tier: production

critical_routes:
  - /api/checkout
  - /api/payment

healing_policy:
  auto_rollback: true
  max_auto_actions_per_hour: 10
  require_approval_for:
    - database_migrations
    - environment_variable_changes

predictive:
  traffic_spike_tables:
    - orders
    - products
  pre_scale_threshold: 2.5

notifications:
  slack_webhook: ${SLACK_WEBHOOK}
  page_sre_after: 3
```

Commit and push — NEXUS reads this on every push via the Git webhook.

---

### Step 5 — (Optional) Annotate Critical Code

```python
from selfheal import SelfHeal

# Mark a route as critical — never drop under load, queue if failing
@app.post("/api/payment")
@SelfHeal.critical(never_shed=True, fallback="queue", alert_sre_after=2)
async def process_payment(req):
    ...

# Mark a DB query as a traffic predictor
@SelfHeal.query(label="order_history", spike_indicator=True, cache_on_failure=True)
def get_orders(user_id):
    return db.query("SELECT * FROM orders WHERE user_id = ?", user_id)

# Wrap your DB pool for automatic query telemetry
from sqlalchemy import create_engine
engine = SelfHeal.wrapDB(create_engine(DATABASE_URL), slow_ms=200, track_tables=True)
```

---

### Step 6 — Open Your Dashboard

```
https://your-nexus-server.com/dashboard
# or open deploy/nexus/developer-dashboard.html in your browser
```

You'll see:
- Every healing action NEXUS took, in plain English
- Traffic predictions based on your DB query patterns
- Live policy editor — override `selfheal.yaml` rules without a git push

---

## Part 2: Deploying NEXUS for Other Developers

### Option 0 — Hardened Docker Image (Recommended — no repo needed)

NEXUS is published as a pre-built image to GitHub Container Registry.
A developer needs **only one file** and **no access to the source repo**.

```bash
# Download the standalone compose file (one-time)
curl -O https://raw.githubusercontent.com/VatsalUmrania/predictive_pod_autoscaler/main/deploy/nexus/nexus-standalone.yaml

# Start everything (NATS + NEXUS API + Prometheus + Grafana)
docker compose -f nexus-standalone.yaml up -d

# Optional: set your Gemini API key for AI-powered RCA
NEXUS_GEMINI_API_KEY=your_key docker compose -f nexus-standalone.yaml up -d
```

That's it. No cloning, no building, no config files to edit.

| Service | URL |
|---------|-----|
| NEXUS API + Swagger UI | http://localhost:8080/docs |
| Developer Dashboard | Open `developer-dashboard.html` pointing to `:8080` |
| Grafana | http://localhost:3000 (admin / nexus_admin) |
| Prometheus | http://localhost:9090 |

**Update to the latest version:**
```bash
docker compose -f nexus-standalone.yaml pull && docker compose -f nexus-standalone.yaml up -d
```

**The image is auto-published** to `ghcr.io/vatsalumrania/nexus-api:latest` on every push to `main` via GitHub Actions (`.github/workflows/docker-publish.yml`).

---

### Option A — VPS / Cloud VM (Team use with custom config)

**Requirements:** Any Linux VM with Docker (2 vCPU, 4 GB RAM minimum)
AWS EC2 `t3.medium`, DigitalOcean Droplet, or Google Cloud `e2-medium` all work.

```bash
# 1. SSH into your server
ssh user@your-server-ip

# 2. Clone the repo
git clone https://github.com/VatsalUmrania/predictive_pod_autoscaler.git
cd predictive_pod_autoscaler

# 3. Create environment file
cp deploy/nexus/.env.example deploy/nexus/.env
nano deploy/nexus/.env   # fill in your values

# 4. Start the full stack
cd deploy/nexus
docker compose -f docker-compose.dev.yaml up -d

# 5. Open ports in your cloud firewall:
#    8080 → NEXUS API
#    3000 → Grafana
#    4222 → NATS (only if developers need direct NATS access)
```

Developers then use:
```bash
export NEXUS_API_URL=http://your-server-ip:8080
export SELFHEAL_TOKEN=sh_xxx   # issued by NEXUS on registration
```

---

### Option B — Domain + HTTPS (Production)

Add Nginx as a reverse proxy in front of the stack:

```nginx
# /etc/nginx/sites-available/nexus
server {
    listen 443 ssl;
    server_name nexus.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/nexus.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nexus.your-domain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
# Get a free SSL cert
certbot --nginx -d nexus.your-domain.com
```

Developers then use:
```bash
export NEXUS_API_URL=https://nexus.your-domain.com
```

---

### Option C — Quick Demo Access (ngrok)

For a live presentation without a server:

```bash
# Terminal 1 — start the stack locally
cd deploy/nexus
docker compose -f docker-compose.dev.yaml up -d

# Terminal 2 — expose NEXUS API publicly
ngrok http 8080
# → Forwarding: https://abc123.ngrok.io → localhost:8080

# Share with developers:
export NEXUS_API_URL=https://abc123.ngrok.io
```

---

### Option D — Kubernetes (Production-grade)

```bash
# Add the Helm repo (once published)
helm repo add nexus https://vatsalumrania.github.io/nexus-helm

# Install
helm install nexus nexus/nexus-selfheal \
  --set nexusApi.replicaCount=2 \
  --set grafana.enabled=true \
  --set nats.replicaCount=3

# The K8s operator is installed automatically and begins
# watching all pods in the cluster immediately.
```

---

### Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXUS_API_URL` | ✅ | URL of your NEXUS server (e.g. `https://nexus.your-domain.com`) |
| `SELFHEAL_TOKEN` | ✅ | Token issued by NEXUS for your app |
| `NEXUS_GEMINI_API_KEY` | Optional | Enables Gemini 1.5 Flash for enhanced RCA |
| `SLACK_WEBHOOK` | Optional | Slack webhook for SRE alerts |
| `NEXUS_AUDIT_DB_PATH` | Server-side | Path to audit trail SQLite (default: `data/nexus_audit.db`) |
| `NATS_URL` | Server-side | NATS connection string (default: `nats://localhost:4222`) |

---

### Publishing the SDK to PyPI (next step)

Once you want any developer to install without cloning the repo:

```bash
# Build the package
cd predictive_pod_autoscaler
pip install build twine
python -m build

# Upload to PyPI
twine upload dist/*

# Developers can then do:
pip install nexus-selfheal
```

The `pyproject.toml` already has the package metadata — just needs a PyPI account and the upload step.
# NEXUS Self-Healing System — Developer Integration Guide

> **NEXUS** monitors your app in real-time, automatically diagnoses failures, and heals them — using AI-powered Root Cause Analysis backed by Gemini, OpenAI, or Anthropic Claude.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Run NEXUS](#2-run-nexus)
3. [Register Your App](#3-register-your-app)
4. [Add selfheal.yaml to Your Repo](#4-add-selfhealyaml-to-your-repo)
5. [Install the Backend SDK](#5-install-the-backend-sdk)
6. [Add the Frontend Beacon](#6-add-the-frontend-beacon)
7. [Connect GitHub Actions](#7-connect-github-actions)
8. [Configure LLM for AI-powered RCA](#8-configure-llm-for-ai-powered-rca)
9. [Open the Dashboard](#9-open-the-dashboard)
10. [Verify Everything Works](#10-verify-everything-works)
11. [Reference — Environment Variables](#11-reference--environment-variables)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

| Requirement | Version |
|-------------|---------|
| Docker + Docker Compose | v24+ |
| Python (your backend) | 3.9+ |
| GitHub repository | Any visibility |
| LLM API key *(optional)* | Gemini / OpenAI / Anthropic |

> **No Kubernetes required** for basic self-healing. NEXUS works with any HTTP-based backend.

---

## 2. Run NEXUS

Create a `docker-compose.yml` in any directory (e.g. your infra repo):

```yaml
version: "3.9"
services:

  nexus-nats:
    image: nats:2.10-alpine
    command: ["--jetstream", "--http_port", "8222"]
    ports:
      - "4222:4222"
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8222/healthz"]
      interval: 5s
      retries: 5

  nexus-api:
    image: ghcr.io/vatsalumrania/nexus-api:latest
    ports:
      - "8080:8080"
    depends_on:
      nexus-nats:
        condition: service_healthy
    environment:
      NATS_URL:             nats://nexus-nats:4222
      # ── LLM Provider (pick one) ──────────────────────────
      NEXUS_LLM_PROVIDER:   gemini          # gemini | openai | anthropic | none
      NEXUS_LLM_API_KEY:    your_api_key_here
      NEXUS_LLM_MODEL:      gemini-1.5-flash  # optional override
      # ────────────────────────────────────────────────────
      NEXUS_AUDIT_DB_PATH:  /data/nexus_audit.db
    volumes:
      - nexus_data:/data
      - ./selfheal.yaml:/app/selfheal.yaml:ro   # your app's policy file

volumes:
  nexus_data:
```

```bash
docker compose up -d
```

NEXUS is now running at **http://localhost:8080**.

---

## 3. Register Your App

Run this once to get your `SELFHEAL_TOKEN`:

```bash
curl -X POST http://localhost:8080/apps/register \
  -H "Content-Type: application/json" \
  -d '{
    "app_name": "my-app",
    "tier":     "production"
  }'
```

Response:
```json
{
  "app_name": "my-app",
  "token": "sh_AbCdEf..."
}
```

> **Save this token** — add it as `SELFHEAL_TOKEN` in your app's environment and as a GitHub Secret.

---

## 4. Add `selfheal.yaml` to Your Repo

Create `selfheal.yaml` in the root of your application repository:

```yaml
app:  my-app
tier: production

healing_policy:
  auto_rollback:              true
  max_auto_actions_per_hour:  10
  require_approval_for:
    - rollout_undo           # require human approval for rollbacks
    - scale_down             # require approval before scaling down

critical_routes:
  - /api/checkout
  - /api/payments

predictive:
  pre_scale_threshold:  2.0   # pre-scale when 2× traffic predicted
  traffic_spike_tables:
    - orders
    - sessions

notifications:
  page_sre_after:  3          # escalate after 3 consecutive failures
  slack_webhook:   "${SLACK_WEBHOOK}"
```

Mount or reference this file when running NEXUS (already done in Step 2 via volume mount).

---

## 5. Install the Backend SDK

### Python

```bash
pip install nexus-selfheal
# or from PyPI equivalent: pip install ppa[sdk]
# or install from github
pip install "ppa[nexus] @ git+https://github.com/VatsalUmrania/predictive_pod_autoscaler.git"
```

Add to your app startup:

```python
from nexus.integration.sdk import NexusSDK

sdk = NexusSDK(
    app_name = "my-app",
    token    = os.environ["SELFHEAL_TOKEN"],
    nexus_url= os.environ.get("NEXUS_URL", "http://localhost:8080"),
)

# Register on startup (sends a heartbeat)
await sdk.heartbeat(status="healthy")
```

### Report errors from your exception handlers

```python
# FastAPI example
@app.exception_handler(Exception)
async def global_error_handler(request, exc):
    await sdk.emit_event("route_error", {
        "route":       str(request.url.path),
        "method":      request.method,
        "error":       str(exc),
        "status_code": 500,
    })
    raise exc
```

### Report slow DB queries

```python
import time

async def get_user(user_id: int):
    start = time.monotonic()
    result = await db.fetchone("SELECT * FROM users WHERE id = ?", user_id)
    elapsed_ms = (time.monotonic() - start) * 1000

    if elapsed_ms > 200:   # report queries over 200ms
        await sdk.emit_event("db_query", {
            "table":       "users",
            "duration_ms": elapsed_ms,
            "query_type":  "SELECT",
        })
    return result
```

### Node.js / Express

```javascript
const { NexusSDK } = require('@nexus/selfheal-sdk');  // coming soon

const sdk = new NexusSDK({
  appName:  'my-app',
  token:    process.env.SELFHEAL_TOKEN,
  nexusUrl: process.env.NEXUS_URL || 'http://localhost:8080',
});

// Report unhandled errors
process.on('uncaughtException', async (err) => {
  await sdk.emitEvent('route_error', { error: err.message });
});
```

> Until the Node SDK is published, use plain HTTP:
> ```bash
> curl -X POST http://localhost:8080/sdk/event \
>   -H "Authorization: Bearer $SELFHEAL_TOKEN" \
>   -H "Content-Type: application/json" \
>   -d '{"type":"route_error","data":{"route":"/api/orders","error":"DB timeout"}}'
> ```

---

## 6. Add the Frontend Beacon

Add one `<script>` tag to your HTML — NEXUS automatically captures:
- JavaScript errors & unhandled promise rejections
- Route changes (SPA navigation)
- Core Web Vitals (LCP, FID, CLS)
- Failed `fetch()` and XHR calls

```html
<head>
  <!-- Add anywhere in <head> -->
  <script
    src="http://localhost:8080/sdk/beacon.js"
    data-app="my-app"
    data-token="sh_AbCdEf..."
    defer
  ></script>
</head>
```

Replace `localhost:8080` with your NEXUS server URL in production.

### Verify it's working

Open your browser DevTools → Network tab → look for requests to `/sdk/event` after any page error occurs.

---

## 7. Connect GitHub Actions

Add the NEXUS workflow to your repository:

```bash
mkdir -p .github/workflows
```

Create `.github/workflows/nexus.yml`:

```yaml
name: NEXUS Deploy Notification
on:
  push:
    branches: ["main", "production"]

jobs:
  notify-nexus:
    runs-on: ubuntu-latest
    steps:
      - name: Notify NEXUS — deploy started
        run: |
          curl -sf -X POST "${{ secrets.NEXUS_API_URL }}/sdk/event" \
            -H "Authorization: Bearer ${{ secrets.NEXUS_SELFHEAL_TOKEN }}" \
            -H "Content-Type: application/json" \
            -d '{
              "type": "deploy_event",
              "data": {
                "commit": "${{ github.sha }}",
                "branch": "${{ github.ref_name }}",
                "author": "${{ github.actor }}"
              }
            }'
        continue-on-error: true
```

### Add GitHub Secrets

1. Go to your repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add:

| Secret Name | Value |
|---|---|
| `NEXUS_API_URL` | `https://nexus.yourdomain.com` (your NEXUS URL) |
| `NEXUS_SELFHEAL_TOKEN` | The token from Step 3 |

> **Why this matters**: NEXUS correlates deploy events with error spikes. If your error rate jumps within 5 minutes of a push, NEXUS classifies it as `bad_deploy` and can automatically roll it back.

---

## 8. Configure LLM for AI-powered RCA

NEXUS supports **Gemini, OpenAI, and Anthropic** out of the box. Set environment variables to enable AI-powered Root Cause Analysis:

### Option A — Google Gemini (recommended, free tier available)

```bash
NEXUS_LLM_PROVIDER=gemini
NEXUS_LLM_API_KEY=your_gemini_api_key     # from aistudio.google.com
NEXUS_LLM_MODEL=gemini-1.5-flash          # or gemini-1.5-pro
```

Get a free API key at: https://aistudio.google.com/apikey

### Option B — OpenAI GPT

```bash
NEXUS_LLM_PROVIDER=openai
NEXUS_LLM_API_KEY=sk-...                  # from platform.openai.com
NEXUS_LLM_MODEL=gpt-4o-mini              # or gpt-4o, gpt-3.5-turbo
```

### Option C — Anthropic Claude

```bash
NEXUS_LLM_PROVIDER=anthropic
NEXUS_LLM_API_KEY=sk-ant-...              # from console.anthropic.com
NEXUS_LLM_MODEL=claude-3-haiku-20240307  # or claude-3-5-sonnet-20241022
```

### Option D — Rule-based only (no LLM, no API key needed)

```bash
NEXUS_LLM_PROVIDER=none
```

NEXUS will still detect and heal incidents using deterministic rules — LLM only improves RCA accuracy.

> **Auto-detection**: If `NEXUS_LLM_PROVIDER` is not set, NEXUS checks for `NEXUS_GEMINI_API_KEY`, then `OPENAI_API_KEY`, then `ANTHROPIC_API_KEY` automatically.

---

## 9. Open the Dashboard

Visit **http://localhost:8080** in your browser.

The NEXUS Developer Dashboard shows:

| Tab | What it shows |
|---|---|
| 🔧 **Incident Feed** | All healing actions — what happened, when, outcome. Approve/reject pending actions. |
| 📈 **Predictions** | Traffic forecasts from the Prescaler — when to expect spikes |
| ⚙️ **Policy Editor** | View and override `selfheal.yaml` settings at runtime without redeploying |

---

## 10. Verify Everything Works

Run through this checklist after setup:

### ✅ NEXUS is running
```bash
curl http://localhost:8080/health
# → {"status":"ok","uptime_seconds":42}
```

### ✅ App is registered
```bash
curl http://localhost:8080/apps
# → [{"app_name":"my-app","tier":"production",...}]
```

### ✅ SDK heartbeat works
```bash
curl -X POST http://localhost:8080/sdk/heartbeat \
  -H "Authorization: Bearer $SELFHEAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"healthy","latency_p99":45}'
# → {"status":"ok"}
```

### ✅ Send a test error event
```bash
curl -X POST http://localhost:8080/sdk/event \
  -H "Authorization: Bearer $SELFHEAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"route_error","data":{"route":"/api/test","error":"test error","status_code":500}}'
```

Then open the dashboard at `http://localhost:8080` — you should see the event appear in the **Incident Feed** within 15 seconds.

### ✅ LLM is active (optional)
```bash
curl http://localhost:8080/status | python -m json.tool | grep -A3 llm
# → "provider": "gemini", "has_api_key": true
```

---

## 11. Reference — Environment Variables

### NEXUS Server

| Variable | Default | Description |
|---|---|---|
| `NEXUS_LLM_PROVIDER` | auto | `gemini` \| `openai` \| `anthropic` \| `none` |
| `NEXUS_LLM_API_KEY` | — | API key for the selected LLM provider |
| `NEXUS_LLM_MODEL` | provider default | Model name override |
| `NEXUS_RCA_TIMEOUT_S` | `10` | Max seconds to wait for LLM response |
| `NEXUS_GEMINI_API_KEY` | — | Legacy: same as `NEXUS_LLM_API_KEY` for Gemini |
| `NATS_URL` | `nats://nats:4222` | Internal message bus URL |
| `NEXUS_AUDIT_DB_PATH` | `/data/nexus_audit.db` | SQLite audit trail path |
| `NEXUS_DASHBOARD_DB_PATH` | `/data/dashboard_incidents.db` | Dashboard incident store |

### Your Application

| Variable | Description |
|---|---|
| `SELFHEAL_TOKEN` | Token from `POST /apps/register` |
| `NEXUS_URL` | URL of your NEXUS instance |

---

## 12. Troubleshooting

### "No incidents showing in dashboard"
1. Check NEXUS API logs: `docker compose logs nexus-api -f`
2. Verify the heartbeat endpoint returns 200: `curl -X POST http://localhost:8080/sdk/heartbeat ...`
3. Check your `SELFHEAL_TOKEN` is correct — it should start with `sh_`

### "LLM not working / RCA shows rule_based source"
1. Confirm `NEXUS_LLM_API_KEY` is set and valid
2. Check logs for `[LLM] Provider:` to see what was detected
3. Test the API key directly with the provider's own API playground
4. RCA still works without LLM — it just uses deterministic rules

### "GitHub Actions not triggering NEXUS"
1. Confirm `NEXUS_API_URL` is accessible from GitHub's runners (must be public or VPN-connected)
2. For local testing use [ngrok](https://ngrok.com): `ngrok http 8080` → use the ngrok URL as `NEXUS_API_URL`
3. Check the Actions run log for the curl output

### "Frontend beacon not sending events"
1. Check browser console for CORS errors
2. Verify the `data-token` attribute is set on the `<script>` tag
3. Ensure NEXUS is accessible from the browser (not just from your server)

### "Port 8080 already in use"
Change the port mapping in `docker-compose.yml`:
```yaml
ports:
  - "9090:8080"   # map to 9090 on host
```
Then update all references from `localhost:8080` to `localhost:9090`.

---

## Need Help?

- **Dashboard**: `http://localhost:8080`
- **API Docs** (Swagger): `http://localhost:8080/docs`
- **Incidents API**: `GET http://localhost:8080/developer/incidents`
- **GitHub Issues**: https://github.com/VatsalUmrania/predictive_pod_autoscaler/issues
