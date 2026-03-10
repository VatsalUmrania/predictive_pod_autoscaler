# PPA — Command Reference Sheet

**Predictive Pod Autoscaler | Semester 6 | March 2026**

---

## Quick Reference — By Task

### 🚀 Get Started Quickly

| Task | Command | Reference |
|------|---------|-----------|
| **Deploy operator to Minikube** | `./scripts/ppa_redeploy.sh --retrain --epochs 100` | [Deployment Guide](../operator/deployment.md) |
| **Train ML models** | `python model/pipeline.py --csv data-collection/training-data/training_data_v2.csv --epochs 50` | [ML Commands](./ml_commands.md) |
| **Check operator health** | `kubectl get ppa` | [Operator Commands](./operator_commands.md) |
| **Watch operator scaling** | `kubectl logs -l app=ppa-operator -f` | [Operator Commands](./operator_commands.md) |
| **See detailed ML guide** | Read [ML Commands](./ml_commands.md) | In-depth training & evaluation |
| **See detailed operator guide** | Read [Operator Commands](./operator_commands.md) | In-depth deployment & debugging |

---

## Detailed Command References

👉 **[ML Pipeline Commands](./ml_commands.md)**
- Training with `model/train.py`
- Evaluation with `model/evaluate.py`
- Conversion to TFLite with `model/convert.py`
- Full pipeline orchestration with `model/pipeline.py`
- Champion-challenger promotion
- Data validation

👉 **[Operator Commands](./operator_commands.md)**
- One-command deployment: `./scripts/ppa_redeploy.sh` (retrain + convert + deploy)
- Configuration via environment variables
- Monitoring & status checks
- Troubleshooting Prometheus, models, scaling
- Health probes & error handling
- Multi-CR management

---

## How to Use the Redeploy Script

```bash
# Retrain + convert + deploy (full pipeline after data collection)
./scripts/ppa_redeploy.sh --retrain --epochs 100

# Deploy existing champion (no retraining)
./scripts/ppa_redeploy.sh

# Fast iteration (skip Docker rebuild)
./scripts/ppa_redeploy.sh --retrain --skip-build

# Non-interactive (don't ask about HPA)
./scripts/ppa_redeploy.sh --delete-hpa

# Help
./scripts/ppa_redeploy.sh --help
```

---

## Individual Commands by Step

### Step 1 — Check Prerequisites
```bash
docker --version
kubectl version --client
helm version
python3 --version
locust --version
pip install locust pandas requests
```

---

### Step 2 — Start Minikube
```bash
# Start (KVM2 — required for this project)
minikube start --driver=kvm2 --cpus=4 --memory=8192

# Check status
minikube status

# Stop (data is preserved)
minikube stop

# NEVER run this — deletes all data
# minikube delete
```

---

### Step 3 — Minikube Addons
```bash
# Enable addons
minikube addons enable metrics-server
minikube addons enable ingress

# Fix metrics-server TLS for minikube
kubectl patch deployment metrics-server -n kube-system \
  --type='json' \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

# Verify
kubectl top nodes
kubectl top pods --all-namespaces
```

---

### Step 4 — Prometheus Stack
```bash
# Add helm repo
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

# Install
kubectl create namespace monitoring
helm install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.adminPassword=admin123 \
  --set prometheus.prometheusSpec.retention=30d \
  --set prometheus.prometheusSpec.scrapeInterval=15s

# Check status
kubectl get pods -n monitoring

# Upgrade (if already installed)
helm upgrade prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.adminPassword=admin123 \
  --set prometheus.prometheusSpec.retention=30d \
  --set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce" \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=15Gi

# Uninstall
helm uninstall prometheus -n monitoring
```

---

### Step 5 — Build & Deploy Instrumented Test App
```bash
# Build image inside minikube's Docker daemon
eval $(minikube docker-env)
docker build -t test-app:latest data-collection/test-app/

# Deploy (Deployment + Service + PodMonitor)
kubectl apply -f data-collection/test-app-deployment.yaml

# Verify pod is running (should show 1/1 — single container, no sidecars)
kubectl get pods -l app=test-app

# Restart with new image
kubectl rollout restart deployment/test-app

# Check logs
kubectl logs -l app=test-app

# Delete and redeploy
kubectl delete -f data-collection/test-app-deployment.yaml
kubectl apply -f data-collection/test-app-deployment.yaml
```

---

### Step 6 — In-Cluster Variable Traffic Generator (Locust)
The Locust traffic generator runs **inside the cluster** to constantly generate phased load for the HorizontalPodAutoscaler.

```bash
# Deploy (sends aggressive scaling traffic to test-app service)
kubectl apply -f deploy/traffic-gen-deployment.yaml

# Check it's running
kubectl get pods -l app=traffic-gen
kubectl logs -l app=traffic-gen -c traffic-gen

# Delete if needed
kubectl delete deployment traffic-gen
```

---

### Step 6.5 — Fixed Replica Scale Profiling (Chaos Testing)
For generating boundary scaling data, you can temporarily disable the HPA and run the headless Locust tester with the `ChaoticLoadShape`.

```bash
# Run chaotic tests against locked replica counts (2, 5, 10, 20)
source venv/bin/activate
./scripts/fixed_replica_test.sh
```

---

### Step 8, 9, 10 — Automated by Script
The `ppa_startup.sh` script handles:
- **Step 8**: Port Forward Watchdog (auto-restarts dead port-forwards)
- **Step 9**: Feature Verification (waits for metrics to populate)
- **Step 10**: CronJob Deployment (hourly data collection)

---

### Manual Validation
After extracting the CSV from Prometheus, pass it through the ML quality gates to ensure model readiness:
```bash
venv/bin/python data-collection/validate_training_data.py data-collection/training-data/training_data_v2.csv
```

---

### Step 7 — Port Forwards
```bash
# Start all port-forwards
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &
kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &
kubectl port-forward svc/test-app 8080:80 -n default &

# Test they work
curl -s http://localhost:9090/-/ready    # → Prometheus Server is Ready.
curl -s http://localhost:3000/api/health # → {"commit":"...","database":"ok",...}
curl -s http://localhost:8080            # → OK

# Kill all port-forwards
pkill -f "port-forward.*9090"
pkill -f "port-forward.*3000"
pkill -f "port-forward.*8080"

# Start watchdog (auto-restarts dead port-forwards)
nohup bash data-collection/keep_portforwards.sh > /tmp/ppa_watchdog.log 2>&1 &

# Check watchdog logs
tail -f /tmp/ppa_watchdog.log
```

---

## Daily Operations

### Verify All 14 Features
```bash
cd /run/media/vatsal/Drive/Projects/predictive_pod_autoscaler
python3 data-collection/verify_features.py
```

### Export Training Data CSV
The data collection python script pulls natively from Prometheus. We use the virtual environment to execute it.

```bash
# Verify v2 schema compliance before training
python data-collection/export_training_data.py \
  --hours 1 \
  --dry-run \
  --assert-schema v2

# This should be the FIRST command any new team member runs
# after cloning the repo

# High-Density 7-Day export (1 row = 15 seconds) - RECOMMENDED for Full Scale Training
source venv/bin/activate
python data-collection/export_training_data.py --hours 168 --step 15s

# Standard recent export (e.g. after a chaos script run)
source venv/bin/activate
python data-collection/export_training_data.py --hours 2 --step 15s

# Recover legacy 24h data and format with new horizons
source venv/bin/activate
python data-collection/export_training_data.py --hours 24 --step 15s
```

### Check Data Volume in Prometheus
```bash
kubectl exec -n monitoring \
  prometheus-prometheus-kube-prometheus-prometheus-0 \
  -- df -h /prometheus
```

### Check All Pods Healthy
```bash
kubectl get pods --all-namespaces
kubectl get pods -n default        # test-app (1/1), traffic-gen (1/1)
kubectl get pods -n monitoring     # prometheus, grafana, alertmanager
```

---

## Prometheus Queries — 14 Input Features + Targets

## Prometheus Queries — 14 Input Features + Targets

| Feature | Source / Details |
|---|---|
| rps_per_replica | App RPM per replica |
| cpu_utilization_pct | cAdvisor CPU relative to limits |
| memory_utilization_pct | cAdvisor memory set relative to limits |
| latency_p95_ms | App P95 latency |
| replicas_normalized | Replicas relative to maxCapacity |
| active_connections | Istio / App Connections |
| error_rate | HTTP 4xx/5xx total errors |
| cpu_acceleration | Rate of change over 5m |
| rps_acceleration | Request rate change over 5m |
| hour_sin | Generated cyclical time |
| hour_cos | Generated cyclical time |
| dow_sin | Generated cyclical time |
| dow_cos | Generated cyclical time |
| is_weekend | Generated binary feature |
| **Targets (y)** | |
| rps_t3m / t5m / t10m | App feature shifted by minutes |
| replicas_t3m / t5m / t10m | Target load capacity ceiling |

---

## Debugging Commands

```bash
# Check what app metrics exist in Prometheus
curl -s "http://localhost:9090/api/v1/label/__name__/values" | python3 -c "
import json, sys
names = json.load(sys.stdin)['data']
app = [n for n in names if 'http_' in n]
for n in app: print(n)
"

# Check Prometheus scrape targets
curl -s "http://localhost:9090/api/v1/targets" | python3 -c "
import json, sys
data = json.load(sys.stdin)
active = data['data']['activeTargets']
for t in active:
    print(t['labels'].get('job','?'), '→', t['health'])
"

# Check PodMonitor is discovered
kubectl get podmonitor -n monitoring

# Verify metrics endpoint directly on pod
POD=$(kubectl get pod -l app=test-app -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward $POD 9091:9091 &
curl -s http://localhost:9091/metrics | head -20

# Restart Prometheus
kubectl rollout restart statefulset prometheus-prometheus-kube-prometheus-prometheus -n monitoring

# Check watchdog logs
tail -f /tmp/ppa_watchdog.log
```

---

## Access URLs

| Service | URL | Credentials |
|---|---|---|
| Prometheus | http://localhost:9090 | none |
| Grafana | http://localhost:3000 | admin / admin123 |
| Test App | http://localhost:8080 | none |

---

## Key Lessons Learned

- Use `prometheus_client` library for direct app instrumentation — zero sidecar dependencies
- PodMonitor needs `release: prometheus` label to match kube-prometheus-stack selector
- Build images inside `eval $(minikube docker-env)` with `imagePullPolicy: Never`
- cAdvisor scrapes without container labels in this setup — use `sum()` without container filter
- Port-forwards die when Prometheus restarts — always run the watchdog
- `[0]` in zsh helm commands needs quoting: `"accessModes[0]=ReadWriteOnce"`
- Multi-line commands in zsh use `\` — if you see `dquote>` press Ctrl+C and run as single line
- Data is stored on `/dev/nvme0n1p8` (your SSD) — safe across reboots, only lost on `minikube delete`
