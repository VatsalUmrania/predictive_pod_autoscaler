# PPA ↔ NEXUS Integration Testing Guide (NATS JetStream)

> End-to-end walkthrough: full PPA infra + demo app as the PPA target → NATS
> → NEXUS Prescaler shadow decisions.

---

## Files Changed / Created by This Guide

| File | Change |
|------|--------|
| `deploy/operator-deployment.yaml` | Added `NATS_URL: nats://host.minikube.internal:4222` |
| `demo/backend/requirements.txt` | Added `prometheus_client>=0.20.0` |
| `demo/backend/main.py` | Added Prometheus Counter/Histogram + `/metrics` route + port 9091 server |
| `demo/backend/deployment.yaml` | **New** — K8s Deployment + Service + HPA + PodMonitor for shop-demo |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Minikube cluster                                                      │
│                                                                        │
│  shop-demo pods                                                        │
│   :8001  HTTP API  ←── traffic (Locust / manual)                      │
│   :9091  /metrics  ←── PodMonitor scrape (15s)                       │
│              │                                                         │
│   kube-prometheus-stack (monitoring ns)                               │
│              │                                                         │
│   ppa-operator  ─── Prometheus queries (14-dim features)             │
│              │                                                         │
│              └── PpaNATSClient.publish()                             │
│                         │                                              │
│           host.minikube.internal:4222  (= demo NATS in Docker)       │
└──────────────────────────────────────────────────────────────────────┘
                           │
                    NATS JetStream
                    stream: PPA_PREDICTIONS
                    subject: ppa.predictions.shop-demo
                           │
┌──────────────────────────────────────────────────────────────────────┐
│  Docker Compose (demo/)                                               │
│                                                                        │
│  nexus-api  ──  Prescaler.subscribe_to_ppa_predictions()             │
│                  handle_spike_prediction()                            │
│                    SHADOW:     log + precision track                  │
│                    ADVISORY:   publish nexus.incidents.*              │
│                    AUTONOMOUS: kubectl scale shop-demo                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

```bash
cd /run/media/vatsal/DATA/Projects/predictive_pod_autoscaler
source venv/bin/activate

# Verify tools
ppa --help
docker info | head -3
minikube version
kubectl version --client
helm version
```

---

## Phase 1 — Start the NEXUS Demo Stack (Docker Compose)

```bash
cd demo
export NEXUS_GEMINI_API_KEY="your-key"   # optional, for LLM RCA

docker compose up -d
docker compose ps   # wait until nexus-api is healthy (~30s)
```

**Smoke test:**
```bash
curl -s http://localhost:8222/healthz          # NATS
curl -s http://localhost:8080/health           # NEXUS API
curl -s http://localhost:8080/ppa/decisions    # should return []
```

---

## Phase 2 — Bootstrap PPA Infra (`ppa init`)

`ppa init` runs 11 steps: prerequisites → Minikube → addons → Prometheus
(Helm) → test-app → Locust → port-forwards → watchdog → ML feature verify
→ data CronJob → chaos baseline.

```bash
cd ..   # back to repo root

# See what will run (no changes)
ppa init --list
ppa init --dry-run

# Run all 11 steps (~5-8 min on first run)
ppa init

# Or step-by-step:
ppa init --step 1   # Prerequisites: docker, kubectl, helm, git
ppa init --step 2   # Minikube start (auto driver, 4 CPU, 8 GB)
ppa init --step 3   # Enable metrics-server + ingress addons
ppa init --step 4   # Install kube-prometheus-stack via Helm
ppa init --step 5   # Build + deploy shop-demo into Minikube
ppa init --step 6   # Deploy Locust traffic generator
ppa init --step 7   # Start port-forwards (Prometheus :9090, Grafana :3000, app :8001, metrics :8000)
ppa init --step 8   # Port-forward watchdog
ppa init --step 9   # Verify 14-dim Prometheus feature set
ppa init --step 10  # Deploy data collection CronJob
ppa init --step 11  # Chaos profiling baseline
```

> [!NOTE]
> Step 5 builds and deploys `demo/backend/deployment.yaml` (Deployment +
> Service + HPA + PodMonitor) — but only if `deploy/demo/` or
> `demo/backend/deployment.yaml` is used as the app path.
> To point `ppa init` at the demo backend use:
> ```bash
> ppa init --app demo/backend
> ```
> Otherwise deploy manually (see §2a below).

### §2a — Deploy shop-demo manually into Minikube

```bash
# 1. Point Docker CLI at Minikube's daemon
eval $(minikube docker-env)

# 2. Build the demo image (from repo root, context = repo root)
docker build -t shop-demo:latest -f demo/backend/Dockerfile .

# 3. Apply all K8s manifests
kubectl apply -f demo/backend/deployment.yaml

# 4. Verify pods running
kubectl get pods -l app=shop-demo -w

# 5. Verify HPA is created
kubectl get hpa shop-demo

# 6. Verify PodMonitor created in monitoring namespace
kubectl get podmonitor -n monitoring
```

### §2b — Verify Prometheus is scraping shop-demo

```bash
# Port-forward Prometheus if not already done
kubectl port-forward -n monitoring svc/prometheus-operated 9090:9090 &

# Wait 60s for first scrape, then query
curl -s 'http://localhost:9090/api/v1/query?query=http_requests_total{job="shop-demo"}' \
  | python -m json.tool | head -20

# Confirm the 14-dim PPA feature set is present
curl -s 'http://localhost:9090/api/v1/query?query=http_request_duration_seconds_count' \
  | python -m json.tool
```

Expected: `"resultType": "vector"` with `value` present.

---

## Phase 3 — Register shop-demo with PPA (`ppa add`)

```bash
ppa add \
  --app-name shop-demo \
  --target   shop-demo \
  --namespace default \
  --min-replicas  1 \
  --max-replicas 10 \
  --rps-capacity 50 \
  --safety-factor 1.15

# Dry run first to see generated manifests
ppa add --app-name shop-demo --target shop-demo --dry-run
```

This generates `PredictiveAutoscaler` CRs for 3m, 5m, 10m horizons and
applies them to the cluster.

```bash
# Verify CRDs applied
kubectl get predictiveautoscalers -n default
# Expected: shop-demo-rps-t3m, shop-demo-rps-t5m, shop-demo-rps-t10m
```

---

## Phase 4 — Train the LSTM Model (`ppa train`)

The model must be trained on historical Prometheus data before the operator
can emit predictions.

```bash
# Collect at least 30 min of data first (CronJob from step 10 does this hourly)
# For immediate testing use the bundled training data:
ppa train \
  --app     shop-demo \
  --horizon rps_t10m \
  --epochs  50 \
  --lookback 60 \
  --patience 20

# Or use the full pipeline command:
ppa run --app shop-demo        # init → add → train → apply in one shot
ppa run --app shop-demo --from train   # resume from train phase only
```

After training:
```bash
ppa model evaluate --app shop-demo    # check SMAPE and accuracy metrics
```

---

## Phase 5 — Deploy the PPA Operator (`ppa apply` / `ppa operator`)

```bash
# Build + deploy operator image into Minikube + apply manifests
ppa apply --app-name shop-demo --yes

# Or use the explicit operator subcommands:
ppa operator build                    # docker build in minikube
ppa operator deploy                   # apply crd + rbac + operator-deployment
ppa operator status                   # check deployment health
ppa operator restart                  # rolling restart (rebuild + redeploy)
```

**Verify NATS_URL is set in the operator deployment:**
```bash
kubectl get deployment ppa-operator -o jsonpath='{.spec.template.spec.containers[0].env}' \
  | python -m json.tool | grep -A2 NATS_URL
# Expected:  "value": "nats://host.minikube.internal:4222"
```

**Verify operator logs show NATS connection:**
```bash
kubectl logs -f deployment/ppa-operator | grep "PPA-NATS"
# Expected:
# [PPA-NATS] Connected to nats://host.minikube.internal:4222
# [PPA-NATS] Created stream 'PPA_PREDICTIONS'
# [PPA-NATS] Published ppa.predictions.shop-demo seq=1
```

---

## Phase 6 — Verify NATS Event Flow

### Check stream state
```bash
# PPA_PREDICTIONS stream (created by PpaNATSClient on connect)
curl -s "http://localhost:8222/jsz?streams=true" | python -m json.tool | grep -E '"name"|"messages"'

# Both streams should show messages > 0 after operator has run 1 cycle (~30s)
```

### Watch live prediction events (Python)
```bash
python - <<'EOF'
import asyncio, json, nats

async def main():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    async def h(msg):
        d = json.loads(msg.data)
        print(f"\n[{msg.subject}]")
        for k in ("deployment","predicted_rps","current_rps","confidence","horizon_minutes","model_version"):
            print(f"  {k:20s}: {d.get(k)}")
        await msg.ack()
    await js.subscribe("ppa.predictions.>", cb=h, durable="test-watcher")
    print("Watching ppa.predictions.>  (Ctrl+C to stop)")
    await asyncio.sleep(300)

asyncio.run(main())
EOF
```

### Use the PPA CLI live dashboard
```bash
ppa monitor --interval 10
# Shows: HPA vs PPA desired replicas, Prometheus metrics, prediction accuracy
```

---

## Phase 7 — Verify NEXUS Prescaler Receives Events

```bash
docker compose -f demo/docker-compose.yaml logs -f nexus-api \
  | grep -i "prescal\|ppa\|prediction"
```

**Expected log lines:**
```
[PreScaler] Subscribed to ppa.predictions.*
[Prescaler] Decision A1B2 [SHADOW] deployment=shop-demo ns=default
            replicas: 2 → 5  rps: 120 → 190  conf=0.72  horizon=10min
[Prescaler] [SHADOW] Would scale shop-demo 2 → 5 (not executed — shadow mode)
```

**Events that are silently skipped:**
```
[Prescaler] Skipped: confidence 0.43 < 0.55   ← confidence gate
[Prescaler] Skipped: increase 12.0% < 30.0%   ← threshold gate
[Prescaler] Skipped: shop-demo in cooldown     ← 300s cooldown
```

---

## Phase 8 — Query the NEXUS API

```bash
# PPA prescale decisions log
curl -s http://localhost:8080/ppa/decisions | python -m json.tool

# Prescaler stats (precision, SMAPE, mode)
curl -s http://localhost:8080/ppa/stats | python -m json.tool

# Full incident audit trail
curl -s http://localhost:8080/developer/incidents | python -m json.tool

# Swagger UI (all 30+ endpoints)
open http://localhost:8080/docs
```

**Expected `/ppa/stats`:**
```json
{
  "mode": "shadow",
  "events_received": 8,
  "decisions_made": 2,
  "skipped_confidence": 4,
  "skipped_threshold": 2,
  "precision": 0.0,
  "smape": null,
  "ready_for_advisory": false
}
```

---

## Phase 9 — Inject a Synthetic PPA Event (Fast Verification)

Bypass the 30s operator cycle to instantly test the full NEXUS pipeline:

```bash
python - <<'EOF'
import asyncio, json, nats
from datetime import datetime, timezone

async def inject():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()

    payload = {
        "deployment":      "shop-demo",
        "namespace":       "default",
        "predicted_rps":   200.0,   # +100% spike → passes 30% threshold gate
        "current_rps":     100.0,
        "confidence":      0.82,    # > 0.55 confidence gate
        "horizon_minutes": 10,
        "model_version":   "test-inject-v1",
        "raw_features": {
            "cpu_usage": 0.72,  "memory_usage": 0.65,
            "rps": 100.0,       "error_rate": 0.01,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    ack = await js.publish(
        "ppa.predictions.shop-demo",
        json.dumps(payload).encode(),
        stream="PPA_PREDICTIONS",
    )
    print(f"✓ Published  seq={ack.seq}")
    await nc.drain()

asyncio.run(inject())
EOF

# Immediately verify
sleep 2
curl -s http://localhost:8080/ppa/decisions | python -m json.tool
docker compose -f demo/docker-compose.yaml logs nexus-api | tail -10
```

---

## Phase 10 — End-to-End Checklist

| # | Check | Command | Pass condition |
|---|-------|---------|----------------|
| 1 | NATS healthy | `curl http://localhost:8222/healthz` | `{"status":"ok"}` |
| 2 | NEXUS API healthy | `curl http://localhost:8080/health` | `200 OK` |
| 3 | shop-demo pods running | `kubectl get pods -l app=shop-demo` | `Running` |
| 4 | PodMonitor created | `kubectl get podmonitor -n monitoring` | `shop-demo` listed |
| 5 | Prometheus scraping | `curl 'http://localhost:9090/api/v1/query?query=http_requests_total'` | `resultType: vector` with data |
| 6 | PPA_PREDICTIONS stream | `curl 'http://localhost:8222/jsz?streams=true'` | `PPA_PREDICTIONS` in list |
| 7 | Operator emitting events | `kubectl logs deployment/ppa-operator \| grep PPA-NATS` | `Published` present |
| 8 | Prescaler subscribed | `docker compose logs nexus-api \| grep Subscribed` | `ppa.predictions.*` line |
| 9 | Decision recorded | `curl http://localhost:8080/ppa/decisions` | Non-empty array |
| 10 | Synthetic inject works | Run inject script + query decisions | New entry appears |

---

## Troubleshooting

### "host.minikube.internal" not resolving inside Minikube pods

```bash
# Verify Minikube's gateway IP (usually 192.168.49.1 on Linux)
minikube ssh -- route | grep default

# If host.minikube.internal DNS doesn't work, patch the operator deployment
# with the concrete gateway IP:
GATEWAY=$(minikube ssh -- route -n | grep '^0.0.0.0' | awk '{print $2}')
kubectl set env deployment/ppa-operator NATS_URL="nats://${GATEWAY}:4222"
kubectl rollout restart deployment/ppa-operator
```

### Prometheus not scraping shop-demo (PodMonitor not picked up)

```bash
# PodMonitor must be in the `monitoring` namespace with label release=prometheus
kubectl get podmonitor shop-demo -n monitoring -o yaml | grep -A3 labels
# Expected: release: prometheus

# Verify Prometheus has discovered the target
curl -s 'http://localhost:9090/api/v1/targets' | python -m json.tool | grep shop-demo
```

### Operator emits events but Prescaler logs nothing

The `PPA_PREDICTIONS` and `NEXUS_INCIDENTS` streams must be on the **same NATS
server**. Verify both the operator and `nexus-api` use `localhost:4222`:

```bash
# Operator NATS_URL
kubectl exec deployment/ppa-operator -- env | grep NATS
# nexus-api NATS_URL
docker compose exec nexus-api env | grep NATS
# Both must resolve to the same nats://... address
```

### All Prescaler events are skipped

Lower the gates temporarily (restart nexus-api after):
```bash
docker compose stop nexus-api
NEXUS_PRESCALE_MIN_CONFIDENCE=0.01 \
NEXUS_PRESCALE_THRESHOLD_PCT=1 \
NEXUS_PRESCALE_COOLDOWN_S=5 \
  docker compose up -d nexus-api
```

### metrics port 9091 not reachable

```bash
# Check prometheus_client server started in logs
docker compose logs shop-backend | grep "Prometheus metrics server"
# Expected: [ShopDemo] 📊 Prometheus metrics server started on :9091

# Direct curl (from host, with port-forward)
kubectl port-forward svc/shop-demo 9091:9091 &
curl http://localhost:9091/metrics | grep http_requests_total
```

---

## Key Environment Variables

| Variable | Default | Set in |
|----------|---------|--------|
| `NATS_URL` (operator) | `nats://host.minikube.internal:4222` | `deploy/operator-deployment.yaml` |
| `NATS_URL` (nexus-api) | `nats://nats:4222` | `demo/docker-compose.yaml` |
| `NEXUS_PRESCALE_MODE` | `shadow` | nexus-api container env |
| `NEXUS_PRESCALE_THRESHOLD_PCT` | `30` | nexus-api container env |
| `NEXUS_PRESCALE_MIN_CONFIDENCE` | `0.55` | nexus-api container env |
| `NEXUS_PRESCALE_COOLDOWN_S` | `300` | nexus-api container env |
| `METRICS_PORT` | `9091` | shop-demo pod env |
| `PPA_TIMER_INTERVAL` | `30` | operator deployment env |

---

## Full PPA CLI Lifecycle Reference

```bash
ppa init --list                           # Show all 11 infra steps
ppa init                                  # Run full cluster bootstrap
ppa init --step 4                         # Run only Prometheus install
ppa init --app demo/backend               # Bootstrap using demo as target app

ppa add --app-name shop-demo \            # Register shop-demo for PPA
        --target shop-demo \
        --rps-capacity 50

ppa train --app shop-demo \               # Train LSTM model
          --horizon rps_t10m \
          --epochs 100

ppa apply --app-name shop-demo --yes      # Build + deploy operator

ppa operator build                        # Rebuild operator image only
ppa operator deploy                       # Apply manifests (CRD+RBAC+deployment)
ppa operator restart                      # Rolling rebuild + redeploy
ppa operator status                       # Check operator pod health

ppa monitor --interval 10                 # Live HPA vs PPA dashboard
ppa watch --app shop-demo                 # Stream live prediction events
ppa status                                # PPA CR + operator status

ppa run --app shop-demo                   # Full pipeline: init→add→train→apply
ppa run --app shop-demo --from train      # Resume from training phase
```
