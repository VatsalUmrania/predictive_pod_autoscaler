# PPA ↔ NEXUS Integration Testing Guide (NATS JetStream)

> End-to-end walkthrough: full PPA infra + demo app as the PPA target → NATS
> → NEXUS Prescaler shadow decisions.

---

## Files Changed / Created by This Guide

| File | Change |
|------|--------|
| `deploy/nexus/nats-statefulset.yaml` | **New** — NATS StatefulSet in `nexus` namespace |
| `deploy/nexus/nexus-api-deployment.yaml` | **New** — nexus-api Deployment in `nexus` namespace |
| `deploy/operator-deployment.yaml` | Changed `NATS_URL` → `nats://nats.nexus.svc.cluster.local:4222` |
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
│           nats.nexus.svc.cluster.local:4222  (K8s service DNS)       │
└──────────────────────────────────────────────────────────────────────┘
                           │
                    NATS JetStream
                    stream: PPA_PREDICTIONS
                    subject: ppa.predictions.shop-demo
                           │
┌──────────────────────────────────────────────────────────────────────┐
│  nexus namespace (in-cluster)                                          │
│                                                                        │
│  nats.nexus.svc.cluster.local:4222   ← PPA operator connects here    │
│                                                                        │
│  nexus-api  ──  Prescaler.subscribe_to_ppa_predictions()             │
│                  handle_spike_prediction()                            │
│                    SHADOW:     log + precision track                  │
│                    ADVISORY:   publish nexus.incidents.*              │
│                    AUTONOMOUS: kubectl scale shop-demo                │
│                                                                        │
│  Developer browser → nexus-api-external NodePort :30081               │
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

## Phase 1 — Deploy NEXUS into Kubernetes

All NEXUS services run inside Minikube — no Docker Compose, no `host.minikube.internal` hacks.

```bash
kubectl apply -f deploy/nexus/nats-statefulset.yaml
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml

# Verify
kubectl get pods -n nexus
# Expected: nats-0 Running, nexus-api-xxxx Running (~30s)

# Developer browser access (no port-forward needed):
#   http://$(minikube ip):30081
```

**Smoke test (from inside the cluster):**
```bash
kubectl exec deployment/ppa-operator -- \
  wget -qO- http://nats.nexus.svc.cluster.local:8222/healthz   # NATS → {"status":"ok"}
kubectl exec -n nexus deployment/nexus-api -- \
  wget -qO- http://localhost:8080/health                      # NEXUS API → 200
```

**For Docker-only (no K8s):** use `demo/docker-compose.standalone.yaml` instead.

---

## Phase 2 — Bootstrap PPA Infra (`ppa init`)

`ppa init` runs 10 steps: prerequisites → Minikube → addons → Prometheus
(Helm) → test-app → Locust → NEXUS K8s manifests → service verify
→ ML feature verify → data CronJob.

> [!NOTE]
> Phase 1 (NEXUS manifests) and Phase 2 (`ppa init` steps 7–8) are both
> required before proceeding to `ppa add`.

```bash
cd ..   # back to repo root

# See what will run (no changes)
ppa init --list
ppa init --dry-run

# Run all 10 steps (~5-8 min on first run)
ppa init

# Or step-by-step:
ppa init --step 1   # Prerequisites: docker, kubectl, helm, git
ppa init --step 2   # Minikube start (auto driver, 4 CPU, 8 GB)
ppa init --step 3   # Enable metrics-server + ingress addons
ppa init --step 4   # Install kube-prometheus-stack via Helm
ppa init --step 5   # Build + deploy shop-demo into Minikube
ppa init --step 6   # Deploy Locust traffic generator
ppa init --step 7   # Apply NATS + nexus-api K8s manifests
ppa init --step 8   # Verify in-cluster NATS + Prometheus reachability
ppa init --step 9   # Verify 14-dim Prometheus feature set
ppa init --step 10  # Deploy data collection CronJob
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
# Query from inside the cluster (operator or any pod in monitoring ns)
kubectl exec deployment/ppa-operator -- \
  wget -qO- 'http://prometheus.monitoring:9090/api/v1/query?query=http_requests_total{job="shop-demo"}' \
  | python -m json.tool | head -20

# Confirm the 14-dim PPA feature set is present
kubectl exec deployment/ppa-operator -- \
  wget -qO- 'http://prometheus.monitoring:9090/api/v1/query?query=http_request_duration_seconds_count' \
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
# Expected:  "value": "nats://nats.nexus.svc.cluster.local:4222"
```

**Verify operator logs show NATS connection:**
```bash
kubectl logs -f deployment/ppa-operator | grep "PPA-NATS"
# Expected:
# [PPA-NATS] Connected to nats://nats.nexus.svc.cluster.local:4222
# [PPA-NATS] Created stream 'PPA_PREDICTIONS'
# [PPA-NATS] Published ppa.predictions.shop-demo seq=1
```

---

## Phase 6 — Verify NATS Event Flow

### Check stream state
```bash
# Port-forward both NATS ports. Run in a separate terminal (stays in foreground):
kubectl port-forward -n nexus svc/nats 4222:4222 8222:8222 &

# PPA_PREDICTIONS stream (created by PpaNATSClient on connect)
sleep 3
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
kubectl logs -n nexus deployment/nexus-api -f | grep -i "prescal\|ppa\|prediction"
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

**Developer browser access** (no port-forward needed):
```bash
open http://$(minikube ip):30081   # NEXUS API + Swagger UI
open http://$(minikube ip):30081/docs  # Swagger UI (all 30+ endpoints)
```

**CLI access:**
```bash
NEXUS=http://$(minikube ip):30081

# PPA prescale decisions log
curl -s $NEXUS/ppa/decisions | python -m json.tool

# Prescaler stats (precision, SMAPE, mode)
curl -s $NEXUS/ppa/stats | python -m json.tool

# Full incident audit trail
curl -s $NEXUS/developer/incidents | python -m json.tool
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

Bypass the 30s operator cycle to instantly test the full PPA→NEXUS pipeline.

**Requires the NATS port-forward running first:**
```bash
# In a separate terminal — forwards K8s NATS to localhost
kubectl port-forward -n nexus svc/nats 4222:4222 8222:8222 &
```

```bash
python - <<'EOF'
import asyncio, json, nats
from datetime import datetime, timezone

async def inject():
    # Connect via port-forward (host → K8s NATS at nats.nexus.svc.cluster.local:4222)
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
NEXUS=http://$(minikube ip):30081
sleep 2
curl -s $NEXUS/ppa/decisions | python -m json.tool
kubectl logs -n nexus deployment/nexus-api | tail -10
```

---

## Phase 9 — End-to-End Checklist

| # | Check | Command | Pass condition |
|---|-------|---------|----------------|
| 1 | NATS healthy | `kubectl exec deploy/nats -n nexus -- wget -qO- http://localhost:8222/healthz` | `{"status":"ok"}` |
| 2 | NEXUS API healthy | `kubectl exec deploy/nexus-api -n nexus -- wget -qO- http://localhost:8080/health` | `200 OK` |
| 3 | NEXUS API external | `curl http://$(minikube ip):30081/health` | `200 OK` |
| 4 | shop-demo pods running | `kubectl get pods -l app=shop-demo` | `Running` |
| 5 | PodMonitor created | `kubectl get podmonitor -n monitoring` | `shop-demo` listed |
| 6 | Prometheus scraping | `kubectl exec deploy/ppa-operator -- wget -qO- http://prometheus.monitoring:9090/api/v1/query?query=http_requests_total` | `resultType: vector` with data |
| 7 | PPA_PREDICTIONS stream | `kubectl port-forward -n nexus svc/nats 8222:8222 & sleep 2 && curl 'http://localhost:8222/jsz?streams=true'` | `PPA_PREDICTIONS` in list |
| 8 | Operator emitting events | `kubectl logs deployment/ppa-operator \| grep PPA-NATS \| grep Published` | Non-empty |
| 9 | Prescaler subscribed | `kubectl logs -n nexus deployment/nexus-api \| grep Subscribed` | `ppa.predictions.*` line |
| 10 | Synthetic inject works | Run inject script + `curl $NEXUS/ppa/decisions` | New entry appears |

---

## Troubleshooting

### NATS not reachable from PPA operator

Verify both are using the same NATS service:
```bash
# Operator NATS_URL
kubectl exec deployment/ppa-operator -- env | grep NATS_URL
# Should be: nats://nats.nexus.svc.cluster.local:4222

# nexus-api NATS_URL
kubectl exec deployment/nexus-api -n nexus -- env | grep NATS_URL
# Should be: nats://nats.nexus.svc.cluster.local:4222

# Verify both pods can reach NATS
kubectl exec deployment/ppa-operator -- wget -qO- http://nats.nexus.svc.cluster.local:8222/healthz
kubectl exec deployment/nexus-api -n nexus -- wget -qO- http://nats.nexus.svc.cluster.local:8222/healthz
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
server**. Verify both the operator and `nexus-api` use `nats.nexus.svc.cluster.local`:

```bash
# Operator NATS_URL
kubectl exec deployment/ppa-operator -- env | grep NATS
# nexus-api NATS_URL
kubectl exec deployment/nexus-api -n nexus -- env | grep NATS
# Both must resolve to: nats://nats.nexus.svc.cluster.local:4222
```

### All Prescaler events are skipped

Lower the gates temporarily (patch the deployment and restart):
```bash
kubectl set env deployment/nexus-api -n nexus \
  NEXUS_PRESCALE_MIN_CONFIDENCE=0.01 \
  NEXUS_PRESCALE_THRESHOLD_PCT=1 \
  NEXUS_PRESCALE_COOLDOWN_S=5
kubectl rollout restart deployment/nexus-api -n nexus
```

### metrics port 9091 not scraping

```bash
# Check prometheus_client server started in logs
kubectl logs -l app=shop-demo | grep "Prometheus metrics server"
# Expected: [ShopDemo] Prometheus metrics server started on :9091

# Verify PodMonitor is picked up by Prometheus
curl -s 'http://localhost:9090/api/v1/targets' | python -m json.tool | grep shop-demo
# Or from inside the cluster (ppa-operator has Prometheus access):
kubectl exec deployment/ppa-operator -- \
  wget -qO- 'http://prometheus.monitoring:9090/api/v1/targets' \
  | python -m json.tool | grep shop-demo
```

---

## Key Environment Variables

| Variable | Default | Set in |
|----------|---------|--------|
| `NATS_URL` (operator) | `nats://nats.nexus.svc.cluster.local:4222` | `deploy/operator-deployment.yaml` |
| `NATS_URL` (nexus-api) | `nats://nats.nexus.svc.cluster.local:4222` | `deploy/nexus/nexus-api-deployment.yaml` |
| `NEXUS_API_URL` (shop-demo) | `http://nexus-api.nexus.svc.cluster.local:8080` | `demo/backend/deployment.yaml` |
| `NEXUS_PRESCALE_MODE` | `shadow` | nexus-api container env |
| `NEXUS_PRESCALE_THRESHOLD_PCT` | `30` | nexus-api container env |
| `NEXUS_PRESCALE_MIN_CONFIDENCE` | `0.55` | nexus-api container env |
| `NEXUS_PRESCALE_COOLDOWN_S` | `300` | nexus-api container env |
| `METRICS_PORT` | `9091` | shop-demo pod env |
| `PPA_TIMER_INTERVAL` | `30` | operator deployment env |

---

## Full PPA CLI Lifecycle Reference

```bash
ppa init --list                           # Show all 10 infra steps
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
