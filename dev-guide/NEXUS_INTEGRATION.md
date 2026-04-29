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
      test: ["CMD", "nats-server", "--signal", "status"]
      interval: 5s
      retries: 5

  nexus-api:
    image: ghcr.io/vatsalumrania/predictive_pod_autoscaler/nexus-api:latest
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
