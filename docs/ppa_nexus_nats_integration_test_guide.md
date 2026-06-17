---

## Running the Shop-Demo + NEXUS + PPA Stack

The guide outlines **9 sequential phases**. Here's the condensed, practical version:

### 🔑 Prerequisites
```bash
cd /run/media/vatsal/DATA/Projects/predictive_pod_autoscaler
source venv/bin/activate

# Verify tools are ready
ppa --help && docker info | head -3 && minikube version && helm version
```

---

### Phase 1 — Deploy NEXUS into K8s
```bash
kubectl apply -f deploy/nexus/nats-statefulset.yaml
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml
kubectl get pods -n nexus   # Wait for: nats-0 Running + nexus-api-xxxx Running
```

---

### Phase 2 — Bootstrap PPA Infra
```bash
# Full bootstrap (~5-8 min)
ppa init

# OR, if you only want shop-demo deployed as target:
ppa init --app demo/backend

# If infra already exists, just deploy shop-demo manually:
eval $(minikube docker-env)
docker build -t shop-demo:latest -f demo/backend/Dockerfile .
kubectl apply -f demo/backend/deployment.yaml
kubectl get pods -l app=shop-demo -w
```

---

### Phase 3 — Register shop-demo with PPA
```bash
ppa add \
  --app-name shop-demo \
  --target   shop-demo \
  --namespace default \
  --min-replicas 1 \
  --max-replicas 10 \
  --rps-capacity 50

kubectl get predictiveautoscalers -n default
# Expected: shop-demo-rps-t3m, shop-demo-rps-t5m, shop-demo-rps-t10m
```

---

### Phase 4 — Train the LSTM Model
```bash
ppa train \
  --app shop-demo \
  --horizon rps_t10m \
  --epochs 50 \
  --lookback 60 \
  --patience 20

ppa model evaluate --app shop-demo   # check accuracy
```

---

### Phase 5 — Deploy the PPA Operator
```bash
ppa apply --app-name shop-demo --yes

# Verify NATS_URL is correct:
kubectl get deployment ppa-operator -o jsonpath='{.spec.template.spec.containers[0].env}' \
  | python -m json.tool | grep -A2 NATS_URL
# Must show: "nats://nats.nexus.svc.cluster.local:4222"
```

---

### Phase 6 — Verify Live Events
```bash
# Port-forward NATS (keep this running in a separate terminal)
kubectl port-forward -n nexus svc/nats 4222:4222 8222:8222 &

# Check stream has messages after ~30s
sleep 3 && curl -s "http://localhost:8222/jsz?streams=true" | python -m json.tool | grep -E '"name"|"messages"'

# Watch operator logs for published events
kubectl logs -f deployment/ppa-operator | grep "PPA-NATS"
```

---

### Phase 7 — Verify NEXUS Prescaler is Receiving
```bash
kubectl logs -n nexus deployment/nexus-api -f | grep -i "prescal\|ppa\|prediction"
```
**Expected:**
```
[PreScaler] Subscribed to ppa.predictions.*
[Prescaler] Decision A1B2 [SHADOW] deployment=shop-demo ...
[Prescaler] [SHADOW] Would scale shop-demo 2 → 5 (not executed — shadow mode)
```

---

### ⚡ Fast Verification — Inject a Synthetic Event
> Skip waiting 30s for the real operator cycle — inject a fake PPA event directly:

```bash
# Ensure port-forward is running first (Phase 6), then:
python - <<'EOF'
import asyncio, json, nats
from datetime import datetime, timezone

async def inject():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    payload = {
        "deployment": "shop-demo", "namespace": "default",
        "predicted_rps": 200.0, "current_rps": 100.0,
        "confidence": 0.82, "horizon_minutes": 10,
        "model_version": "test-inject-v1",
        "raw_features": {"cpu_usage": 0.72, "memory_usage": 0.65, "rps": 100.0, "error_rate": 0.01},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    ack = await js.publish("ppa.predictions.shop-demo", json.dumps(payload).encode(), stream="PPA_PREDICTIONS")
    print(f"✓ Published seq={ack.seq}")
    await nc.drain()

asyncio.run(inject())
EOF

# Then check NEXUS received it
NEXUS=http://$(minikube ip):30081
sleep 2 && curl -s $NEXUS/ppa/decisions | python -m json.tool
```

---

### Phase 8 — Query the NEXUS API
```bash
NEXUS=http://$(minikube ip):30081

curl -s $NEXUS/ppa/stats    | python -m json.tool   # prescaler stats
curl -s $NEXUS/ppa/decisions | python -m json.tool  # decision log
open $NEXUS/docs                                      # Swagger UI
```

---

### 🐛 Common Issues

| Issue | Fix |
|---|---|
| Prescaler events all skipped | Lower gates: `kubectl set env deploy/nexus-api -n nexus NEXUS_PRESCALE_MIN_CONFIDENCE=0.01 NEXUS_PRESCALE_THRESHOLD_PCT=1` |
| NATS not reachable | Check `kubectl exec deployment/ppa-operator -- env \| grep NATS_URL` → must be `nats://nats.nexus.svc.cluster.local:4222` |
| PodMonitor not picked up | Ensure it has label `release: prometheus` in `monitoring` namespace |
| No metrics on port 9091 | Check `kubectl logs -l app=shop-demo \| grep "Prometheus metrics server"` |

---

**TL;DR order:** Phase 1 (NEXUS K8s) → `ppa init` (Phase 2) → `ppa add` → `ppa train` → `ppa apply` → verify logs. Use the **synthetic inject script** in Phase 9 for instant end-to-end testing without waiting for the full ML cycle.