# PPA — Command Reference Sheet
**Predictive Pod Autoscaler | Semester 6 | February 2026**

---

## How to Use the Startup Script

```bash
# Make executable (first time only)
chmod +x ppa_startup.sh

# Run everything from scratch
./ppa_startup.sh

# Run a single step only
./ppa_startup.sh --step 5

# List all steps
./ppa_startup.sh --list
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

### Step 5 — Istio
```bash
# Install Istio
curl -L https://istio.io/downloadIstio | sh -
cd istio-1.*
export PATH=$PWD/bin:$PATH
istioctl install --set profile=demo -y

# Check Istio pods
kubectl get pods -n istio-system

# Label namespace for auto-injection
kubectl label namespace default istio-injection=enabled --overwrite

# Verify label
kubectl get namespace default --show-labels

# Apply telemetry config for Prometheus metrics
kubectl apply -f - <<EOF
apiVersion: telemetry.istio.io/v1alpha1
kind: Telemetry
metadata:
  name: mesh-default
  namespace: istio-system
spec:
  metrics:
  - providers:
    - name: prometheus
    overrides:
    - match:
        metric: ALL_METRICS
      disabled: false
EOF

# Check telemetry resource
kubectl get telemetry -n istio-system
```

---

### Step 6 — Test App
```bash
# Deploy
kubectl create deployment test-app --image=nginx --replicas=2
kubectl expose deployment test-app --port=80 --type=ClusterIP

# Verify Istio sidecar injected (MUST show 2/2)
kubectl get pods -l app=test-app

# Restart to force sidecar injection
kubectl rollout restart deployment/test-app

# Delete and redeploy
kubectl delete deployment test-app
kubectl delete service test-app
kubectl create deployment test-app --image=nginx --replicas=2
kubectl expose deployment test-app --port=80 --type=ClusterIP

# Check logs
kubectl logs -l app=test-app -c nginx
kubectl logs -l app=test-app -c istio-proxy
```

---

### Step 7 — In-Cluster Traffic Generator
```bash
# Deploy (sends traffic through Istio mesh — required for HTTP metrics)
kubectl apply -f data-collection/traffic-gen-deployment.yaml

# Check it's running
kubectl get pods -l app=traffic-gen
kubectl logs -l app=traffic-gen -c traffic-gen

# Scale up for more traffic
kubectl scale deployment traffic-gen --replicas=3

# Delete if needed
kubectl delete deployment traffic-gen
```

---

### Step 8 — Prometheus Scrape Config for Istio
```bash
# Create scrape config file
cat > /tmp/istio-scrape.yaml << 'SCRAPEEOF'
- job_name: istio-envoy
  kubernetes_sd_configs:
  - role: pod
  relabel_configs:
  - source_labels: [__meta_kubernetes_pod_label_security_istio_io_tlsMode]
    action: keep
    regex: istio
  - source_labels: [__meta_kubernetes_pod_name]
    target_label: pod
  - source_labels: [__meta_kubernetes_pod_namespace]
    target_label: namespace
  - source_labels: [__meta_kubernetes_pod_ip]
    replacement: $1:15020
    target_label: __address__
  metrics_path: /stats/prometheus
  scheme: http
SCRAPEEOF

# Apply as secret
kubectl delete secret additional-scrape-configs -n monitoring 2>/dev/null || true
kubectl create secret generic additional-scrape-configs \
  --from-file=prometheus-additional.yaml=/tmp/istio-scrape.yaml \
  -n monitoring

# Patch Prometheus to use it
kubectl patch prometheus prometheus-kube-prometheus-prometheus \
  -n monitoring \
  --type merge \
  -p '{"spec":{"additionalScrapeConfigs":{"name":"additional-scrape-configs","key":"prometheus-additional.yaml"}}}'

# Verify config loaded
curl -s http://localhost:9090/api/v1/status/config | python3 -c "
import json, sys
config = json.load(sys.stdin)['data']['yaml']
print('istio-envoy in config:', 'istio-envoy' in config)
print('15020 in config:', '15020' in config)
"
```

---

### Step 9 — Port Forwards
```bash
# Start all port-forwards
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &
kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &
kubectl port-forward svc/test-app 8080:80 -n default &

# Test they work
curl -s http://localhost:9090/-/ready    # → Prometheus Server is Ready.
curl -s http://localhost:3000/api/health # → {"commit":"...","database":"ok",...}
curl -s http://localhost:8080            # → nginx HTML

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

### Verify All 8 Features
```bash
cd /run/media/vatsal/Drive/Projects/predictive_pod_autoscaler
python3 data-collection/verify_features.py
```

### Export Training Data CSV
```bash
python3 data-collection/export_training_data.py
# Output → data-collection/training-data/features_YYYYMMDD_YYYYMMDD.csv
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
kubectl get pods -n default        # test-app (2/2), traffic-gen (2/2)
kubectl get pods -n monitoring     # prometheus, grafana, alertmanager
kubectl get pods -n istio-system   # istiod, ingressgateway, egressgateway
```

---

## Prometheus Queries — All 8 LSTM Features

| Feature | Query |
|---|---|
| requests_per_second | `rate(istio_requests_total{destination_service=~"test-app.*"}[1m])` |
| cpu_usage_percent | `sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))*100` |
| memory_usage_bytes | `sum(container_memory_working_set_bytes{pod=~"test-app.*"})` |
| latency_p95_ms | `histogram_quantile(0.95, rate(istio_request_duration_milliseconds_bucket{destination_service=~"test-app.*"}[5m]))` |
| active_connections | `envoy_server_total_connections{pod=~"test-app.*"}` |
| error_rate | `sum(istio_requests_total{destination_service=~"test-app.*"})` |
| hour_of_day | Generated in Python from timestamp |
| day_of_week | Generated in Python from timestamp |

---

## Debugging Commands

```bash
# Check what Istio metrics exist in Prometheus
curl -s "http://localhost:9090/api/v1/label/__name__/values" | python3 -c "
import json, sys
names = json.load(sys.stdin)['data']
istio = [n for n in names if 'istio' in n]
for n in istio: print(n)
"

# Check metrics directly on pod (bypasses Prometheus)
POD=$(kubectl get pod -l app=test-app -o jsonpath='{.items[0].metadata.name}')
kubectl exec $POD -c istio-proxy -- curl -s localhost:15020/metrics | grep istio_requests_total | head -5

# Check Prometheus scrape targets
curl -s "http://localhost:9090/api/v1/targets" | python3 -c "
import json, sys
data = json.load(sys.stdin)
active = data['data']['activeTargets']
for t in active:
    print(t['labels'].get('job','?'), '→', t['health'])
"

# Check Prometheus can reach pod
POD_IP=$(kubectl get pod -l app=test-app -o jsonpath='{.items[0].status.podIP}')
kubectl exec -n monitoring prometheus-prometheus-kube-prometheus-prometheus-0 \
  -c prometheus -- wget -qO- http://$POD_IP:15020/metrics | grep istio_requests_total | head -3

# Restart Prometheus
kubectl rollout restart statefulset prometheus-prometheus-kube-prometheus-prometheus -n monitoring

# Check Locust traffic logs
tail -f /tmp/locust.log

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

- `kubectl port-forward` traffic **bypasses Istio** — always use in-cluster traffic-gen for metrics
- PodMonitor relabeling silently drops targets — use `additionalScrapeConfigs` secret instead
- cAdvisor scrapes without container labels in this setup — use `sum()` without container filter
- Istio 1.29 requires explicit `Telemetry` resource to enable prometheus metrics
- Port-forwards die when Prometheus restarts — always run the watchdog
- `[0]` in zsh helm commands needs quoting: `"accessModes[0]=ReadWriteOnce"`
- Multi-line commands in zsh use `\` — if you see `dquote>` press Ctrl+C and run as single line
- Data is stored on `/dev/nvme0n1p8` (your SSD) — safe across reboots, only lost on `minikube delete`
