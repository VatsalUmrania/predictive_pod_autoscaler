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
