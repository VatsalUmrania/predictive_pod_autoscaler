## Running the Shop-Demo + NEXUS + PPA Stack

The guide outlines **9 sequential phases**. Here's the condensed, practical version:

### 🔑 Prerequisites
```bash
cd /run/media/vatsal/DATA/Projects/predictive_pod_autoscaler
source venv/bin/activate

# Verify tools are ready
docker info | head -3 && minikube version && helm version && kubectl version --client
```

> Note: the only remaining `ppa` CLI command is `ppa model push` (it orchestrates a loader pod for TFLite transfers into the `ppa-models` PVC). Everything below is plain `kubectl` / `docker` / `helm`.

---

### Phase 1 — Deploy NEXUS into K8s
```bash
# PPA CRD + RBAC + persistent volume claim (declared up front so `ppa model push`
# never has to create the PVC imperatively — eliminates the
# "missing kubectl.kubernetes.io/last-applied-configuration annotation"
# warning from Phase 5; the RBAC binding prevents
# `serviceaccount "ppa-operator" not found` from the operator deployment)
kubectl apply -f deploy/crd.yaml
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/ppa-models-pvc.yaml

# NEXUS (NATS + nexus-api)
kubectl apply -f deploy/nexus/nats-statefulset.yaml
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml
kubectl get pods -n nexus   # Wait for: nats-0 Running + nexus-api-xxxx Running
```

---

### Phase 2 — Bootstrap PPA Infra
```bash
# 1. Start minikube
minikube start --cpus=4 --memory=6g --driver=docker

# 2. Enable K8s addons (for HPA + internal traffic)
minikube addons enable metrics-server
minikube addons enable ingress

# 3. Install Prometheus + Grafana via Helm (kube-prometheus-stack)
kubectl create namespace monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
    -n monitoring \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
# Wait for Prometheus before moving on — the operator (Phase 5) queries it
# on startup. If it isn't up, the operator trips a circuit breaker and needs
# ~5 min to recover on its own.
kubectl wait --for=condition=ready pod -l "release=prometheus" -n monitoring --timeout=300s

# 4. Build & deploy shop-demo (target app) into minikube's docker daemon
eval $(minikube docker-env)
docker build -t shop-demo:latest -f demo/backend/Dockerfile .
kubectl apply -f demo/backend/deployment.yaml
kubectl get pods -l app=shop-demo -w

# 5. (Optional) Deploy synthetic traffic generator
kubectl apply -f deploy/traffic/traffic-gen-deployment.yaml
```

---

### Phase 3 — Register shop-demo with PPA
Apply the three `PredictiveAutoscaler` CRs directly (one per horizon).
Source files at `deploy/generated-manifests/shop-demo/ppa-normalized_rps_t{3,5,10}m.yaml`:
```bash
cat <<EOF | kubectl apply -f -
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t3m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t3m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: true
  modelPath: "/models/shop-demo/normalized_rps_t3m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t3m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t3m/current/target_scaler.pkl"
---
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t5m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t5m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: true
  modelPath: "/models/shop-demo/normalized_rps_t5m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t5m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t5m/current/target_scaler.pkl"
---
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t10m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t10m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: false
  modelPath: "/models/shop-demo/normalized_rps_t10m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t10m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t10m/current/target_scaler.pkl"
EOF

kubectl get predictiveautoscalers -n default
# Expected:
# shop-demo-ppa-normalized-rps-t3m
# shop-demo-ppa-normalized-rps-t5m
# shop-demo-ppa-normalized-rps-t10m
```

Or, equivalently: `kubectl apply -f deploy/generated-manifests/shop-demo/`.

> **Re-run warning**: if you previously deployed older CRs (e.g. named
> `shop-demo-rps-t3m` with `horizon: "rps_t3m"`) whose model bundles no
> longer exist, multiple autoscalers will fight for the same deployment
> and the operator will log
> `Model bundle pending: no model bundle found for shop-demo/rps_t*` plus
> `Multiple CRs detected for target default/shop-demo; active autoscalers
> can fight over replicas`. Delete the stale ones first:
> ```bash
> kubectl get predictiveautoscalers -n default   # list
> kubectl delete predictiveautoscaler <stale-name> -n default
> ```

---

### Phase 4 — Push pre-trained model to PVC
> No training step — using the pre-trained model from `artifacts/shop-demo/`.
> `ppa model push` spawns a loader pod, runs scaler-regeneration in-cluster,
> and copies `model.tflite` + scalers into the `ppa-models` PVC.
```bash
ppa model push \
  --app-name shop-demo \
  --namespace default \
  --horizon normalized_rps_t3m,normalized_rps_t5m,normalized_rps_t10m
```
> Horizons must match the `appName`/`horizon` in the CR (Phase 3) and the
> model directory layout (`/models/<app>/<horizon>/current/`).

---

### Phase 5 — Build & Deploy the PPA Operator
```bash
# Build operator image in minikube's docker daemon (so the cluster can pull it)
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f src/ppa/operator/Dockerfile .

# Apply operator + verify roll-out
kubectl apply -f deploy/operator-deployment.yaml
kubectl rollout status deployment/ppa-operator -n default --timeout=120s

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

### Phase 9 — Test LLM Self-Healing Orchestrator
> Test the new Gemini-based event-driven agent architecture that diagnoses anomalies and proposes fixes via Slack.

```bash
# 1. Apply the new RBAC permissions for the agent
kubectl apply -f infra/nexus-rbac.yaml

# 2. Add API keys to the Nexus API deployment (replace with your real keys)
kubectl set env deployment/nexus-api -n nexus \
  NEXUS_LLM_API_KEY="your_gemini_api_key" \
  SLACK_WEBHOOK_URL="your_slack_webhook_url"

# 3. Wait for pod to restart and inject a synthetic AlertManager webhook
kubectl rollout status deployment/nexus-api -n nexus --timeout=60s
NEXUS=http://$(minikube ip):30081

curl -X POST "$NEXUS/webhook/alertmanager" \
  -H "Content-Type: application/json" \
  -d '{
    "receiver": "nexus-webhook",
    "status": "firing",
    "alerts": [
      {
        "status": "firing",
        "labels": {
          "alertname": "HighErrorRate",
          "severity": "critical",
          "namespace": "default",
          "pod": "shop-demo-54f67c8b9d-abcde"
        },
        "annotations": {
          "description": "Error rate exceeds 5% for shop-demo"
        }
      }
    ]
  }'

# 4. Check the LLM orchestrator logs to verify diagnosis and Slack integration
kubectl logs -n nexus deployment/nexus-api -f | grep -i "llm\|diagnosis\|chatops\|slack"
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

**TL;DR order:** Phase 1 (NEXUS K8s) → Phase 2 (minikube+helm+shop-demo) → Phase 3 (apply PredictiveAutoscaler CRs) → Phase 4 (`ppa model push`) → Phase 5 (build + deploy operator) → verify logs. Use the **synthetic inject script** in the Fast Verification step for instant testing, and run **Phase 9** to test the new Gemini LLM auto-remediation workflow.