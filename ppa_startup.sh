#!/bin/bash
# ============================================================
#  PPA — Predictive Pod Autoscaler
#  Full Startup & Data Collection Script
#  Vatsal Umrania | Semester 6 | February 2026
# ============================================================
# Usage:
#   ./ppa_startup.sh          — run everything automatically
#   ./ppa_startup.sh --step 3 — run only step 3
#   ./ppa_startup.sh --list   — list all steps
# ============================================================

set -e  # exit on error

# ── Colors ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── Config ───────────────────────────────────────────────────
PROJECT_DIR="/run/media/vatsal/Drive/Projects/predictive_pod_autoscaler"
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
APP_PORT=8080

# ── Helpers ──────────────────────────────────────────────────
log()     { echo -e "${GREEN}[✔]${NC} $1"; }
info()    { echo -e "${BLUE}[→]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✘]${NC} $1"; }
heading() { echo -e "\n${BOLD}${CYAN}══ $1 ══${NC}"; }
wait_for_pods() {
    local label=$1
    local namespace=${2:-default}
    local expected=${3:-1}
    info "Waiting for pods with label: $label in namespace: $namespace"
    kubectl wait --for=condition=ready pod \
        -l "$label" \
        -n "$namespace" \
        --timeout=120s && log "Pods ready" || warn "Timeout — check manually"
}

# ── Step List ─────────────────────────────────────────────────
list_steps() {
    echo ""
    echo -e "${BOLD}PPA Startup Steps:${NC}"
    echo ""
    echo "  1  — Check prerequisites (docker, kubectl, helm, python3)"
    echo "  2  — Start Minikube (KVM2 driver)"
    echo "  3  — Enable Minikube addons (metrics-server, ingress)"
    echo "  4  — Install Prometheus stack"
    echo "  5  — Install Istio service mesh"
    echo "  6  — Deploy test-app (nginx with Istio sidecar)"
    echo "  7  — Deploy in-cluster traffic generator"
    echo "  8  — Configure Istio Telemetry for Prometheus"
    echo "  9  — Configure additional scrape config (port 15020)"
    echo "  10 — Start port-forwards (Prometheus + Grafana)"
    echo "  11 — Start port-forward watchdog"
    echo "  12 — Verify all 8 ML features"
    echo "  13 — Export training data to CSV"
    echo ""
}

# ── Parse Args ────────────────────────────────────────────────
SINGLE_STEP=""
if [[ "$1" == "--list" ]]; then list_steps; exit 0; fi
if [[ "$1" == "--step" ]]; then SINGLE_STEP="$2"; fi

run_step() {
    local step_num=$1
    if [[ -n "$SINGLE_STEP" && "$SINGLE_STEP" != "$step_num" ]]; then
        return 0
    fi
    return 1  # means "run this step"
}

# ═══════════════════════════════════════════════════════════════
#  STEP 1 — Prerequisites
# ═══════════════════════════════════════════════════════════════
if ! run_step 1; then
heading "STEP 1 — Checking Prerequisites"

check_cmd() {
    if command -v "$1" &>/dev/null; then
        log "$1 found: $(command -v $1)"
    else
        error "$1 NOT found — please install it first"
        exit 1
    fi
}

check_cmd docker
check_cmd kubectl
check_cmd helm
check_cmd python3
check_cmd git
check_cmd locust || warn "locust not found — installing..."
    pip install locust pandas requests 2>/dev/null && log "Python dependencies installed"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 2 — Start Minikube
# ═══════════════════════════════════════════════════════════════
if ! run_step 2; then
heading "STEP 2 — Starting Minikube (KVM2)"

MINIKUBE_STATUS=$(minikube status --format='{{.Host}}' 2>/dev/null || echo "Stopped")

if [[ "$MINIKUBE_STATUS" == "Running" ]]; then
    log "Minikube already running"
else
    info "Starting minikube with KVM2 driver..."
    minikube start \
        --driver=kvm2 \
        --cpus=4 \
        --memory=8192 \
        --disk-size=20g \
        --kubernetes-version=v1.28.3
    log "Minikube started"
fi

kubectl get nodes
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 3 — Enable Addons
# ═══════════════════════════════════════════════════════════════
if ! run_step 3; then
heading "STEP 3 — Enabling Minikube Addons"

minikube addons enable metrics-server
minikube addons enable ingress

# Fix metrics-server TLS issue on minikube
info "Patching metrics-server for minikube TLS..."
kubectl patch deployment metrics-server \
    -n kube-system \
    --type='json' \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' \
    2>/dev/null || warn "Patch already applied or not needed"

sleep 10
kubectl top nodes 2>/dev/null && log "Metrics server working" || warn "Metrics server still warming up"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 4 — Install Prometheus Stack
# ═══════════════════════════════════════════════════════════════
if ! run_step 4; then
heading "STEP 4 — Installing Prometheus + Grafana Stack"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null
helm repo update

if helm status prometheus -n monitoring &>/dev/null; then
    log "Prometheus already installed — skipping"
else
    kubectl create namespace monitoring 2>/dev/null || true

    helm install prometheus prometheus-community/kube-prometheus-stack \
        --namespace monitoring \
        --set grafana.adminPassword=admin123 \
        --set prometheus.prometheusSpec.retention=30d \
        --set prometheus.prometheusSpec.scrapeInterval=15s \
        --timeout=5m

    info "Waiting for Prometheus pods..."
    sleep 30
    wait_for_pods "app.kubernetes.io/name=prometheus" "monitoring"
    log "Prometheus stack installed"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 5 — Install Istio
# ═══════════════════════════════════════════════════════════════
if ! run_step 5; then
heading "STEP 5 — Installing Istio 1.29"

if kubectl get namespace istio-system &>/dev/null; then
    log "Istio already installed — skipping"
else
    info "Downloading and installing Istio..."
    curl -L https://istio.io/downloadIstio | sh - 2>/dev/null
    ISTIO_DIR=$(ls -d istio-* 2>/dev/null | head -1)
    export PATH="$PWD/$ISTIO_DIR/bin:$PATH"

    istioctl install --set profile=demo -y
    wait_for_pods "app=istiod" "istio-system"
fi

# Label namespace for sidecar injection
kubectl label namespace default istio-injection=enabled --overwrite
log "Namespace labeled for Istio injection"

# Apply Telemetry resource for Prometheus metrics
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
log "Istio Telemetry configured"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 6 — Deploy Test App
# ═══════════════════════════════════════════════════════════════
if ! run_step 6; then
heading "STEP 6 — Deploying Test App (nginx + Istio sidecar)"

if kubectl get deployment test-app -n default &>/dev/null; then
    log "test-app already deployed"
    READY=$(kubectl get pods -l app=test-app -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)
    if [[ "$READY" != "true" ]]; then
        info "Restarting test-app to ensure Istio sidecar is injected..."
        kubectl rollout restart deployment/test-app
    fi
else
    kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: test-app
  namespace: default
spec:
  replicas: 2
  selector:
    matchLabels:
      app: test-app
  template:
    metadata:
      labels:
        app: test-app
    spec:
      containers:
      - name: nginx
        image: nginx:latest
        ports:
        - containerPort: 80
        resources:
          requests:
            cpu: "100m"
            memory: "128Mi"
          limits:
            cpu: "500m"
            memory: "256Mi"
---
apiVersion: v1
kind: Service
metadata:
  name: test-app
  namespace: default
spec:
  selector:
    app: test-app
  ports:
  - port: 80
    targetPort: 80
  type: ClusterIP
EOF
fi

info "Waiting for test-app pods (2/2 with Istio sidecar)..."
sleep 10
wait_for_pods "app=test-app" "default"

CONTAINERS=$(kubectl get pods -l app=test-app -o jsonpath='{.items[0].status.containerStatuses}' 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "?")
if [[ "$CONTAINERS" == "2" ]]; then
    log "Istio sidecar confirmed (2/2 containers)"
else
    warn "Unexpected container count: $CONTAINERS — check manually"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 7 — Deploy In-Cluster Traffic Generator
# ═══════════════════════════════════════════════════════════════
if ! run_step 7; then
heading "STEP 7 — Deploying In-Cluster Traffic Generator"

if kubectl get deployment traffic-gen -n default &>/dev/null; then
    log "traffic-gen already deployed"
else
    kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: traffic-gen
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: traffic-gen
  template:
    metadata:
      labels:
        app: traffic-gen
    spec:
      containers:
      - name: traffic-gen
        image: curlimages/curl:latest
        command:
        - /bin/sh
        - -c
        - |
          echo "Traffic generator started..."
          while true; do
            curl -s http://test-app.default.svc.cluster.local/ > /dev/null
            sleep 0.5
          done
EOF
    wait_for_pods "app=traffic-gen" "default"
    log "In-cluster traffic generator running (~2 req/s through Istio mesh)"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 8 — Configure Istio Telemetry
# ═══════════════════════════════════════════════════════════════
if ! run_step 8; then
heading "STEP 8 — Configuring Istio Telemetry"

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
log "Istio Telemetry resource applied"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 9 — Configure Prometheus Scrape for Istio Port 15020
# ═══════════════════════════════════════════════════════════════
if ! run_step 9; then
heading "STEP 9 — Configuring Prometheus to Scrape Istio (port 15020)"

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

kubectl delete secret additional-scrape-configs -n monitoring 2>/dev/null || true
kubectl create secret generic additional-scrape-configs \
    --from-file=prometheus-additional.yaml=/tmp/istio-scrape.yaml \
    -n monitoring

kubectl patch prometheus prometheus-kube-prometheus-prometheus \
    -n monitoring \
    --type merge \
    -p '{"spec":{"additionalScrapeConfigs":{"name":"additional-scrape-configs","key":"prometheus-additional.yaml"}}}'

log "Scrape config applied — Prometheus will scrape Istio sidecars on port 15020"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 10 — Start Port Forwards
# ═══════════════════════════════════════════════════════════════
if ! run_step 10; then
heading "STEP 10 — Starting Port Forwards"

# Kill existing port-forwards
pkill -f "port-forward.*9090" 2>/dev/null || true
pkill -f "port-forward.*3000" 2>/dev/null || true
pkill -f "port-forward.*8080" 2>/dev/null || true
sleep 2

# Start fresh
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &>/dev/null &
log "Prometheus port-forward started → http://localhost:9090"

kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &>/dev/null &
log "Grafana port-forward started     → http://localhost:3000 (admin/admin123)"

kubectl port-forward svc/test-app 8080:80 -n default &>/dev/null &
log "test-app port-forward started    → http://localhost:8080"

sleep 5

# Verify
if curl -s http://localhost:9090/-/ready | grep -q "Ready"; then
    log "Prometheus responding ✓"
else
    warn "Prometheus not ready yet — may need a moment"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 11 — Port Forward Watchdog
# ═══════════════════════════════════════════════════════════════
if ! run_step 11; then
heading "STEP 11 — Starting Port Forward Watchdog"

cat > /tmp/ppa_watchdog.sh << 'WATCHEOF'
#!/bin/bash
while true; do
    if ! curl -s http://localhost:9090/-/ready > /dev/null 2>&1; then
        echo "$(date) — Restarting Prometheus port-forward..."
        pkill -f "port-forward.*9090" 2>/dev/null
        kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &>/dev/null &
    fi
    if ! curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
        echo "$(date) — Restarting Grafana port-forward..."
        pkill -f "port-forward.*3000" 2>/dev/null
        kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &>/dev/null &
    fi
    sleep 30
done
WATCHEOF

chmod +x /tmp/ppa_watchdog.sh
nohup bash /tmp/ppa_watchdog.sh > /tmp/ppa_watchdog.log 2>&1 &
log "Watchdog running (PID: $!) — auto-restarts dead port-forwards every 30s"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 12 — Verify All 8 ML Features
# ═══════════════════════════════════════════════════════════════
if ! run_step 12; then
heading "STEP 12 — Verifying All 8 ML Features"

info "Waiting 60s for metrics to populate..."
sleep 60

python3 "$PROJECT_DIR/data-collection/verify_features.py" || warn "Some features may still be warming up"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 13 — Export Training Data
# ═══════════════════════════════════════════════════════════════
if ! run_step 13; then
heading "STEP 13 — Exporting Training Data to CSV"

cd "$PROJECT_DIR"
python3 data-collection/export_training_data.py
fi

# ═══════════════════════════════════════════════════════════════
#  DONE
# ═══════════════════════════════════════════════════════════════
if [[ -z "$SINGLE_STEP" ]]; then
    echo ""
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║   PPA Data Collection Stack is Running!  ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}Prometheus${NC}   → http://localhost:9090"
    echo -e "  ${CYAN}Grafana${NC}      → http://localhost:3000  (admin / admin123)"
    echo -e "  ${CYAN}Test App${NC}     → http://localhost:8080"
    echo ""
    echo -e "  ${YELLOW}Daily tasks:${NC}"
    echo -e "    Verify features : python3 data-collection/verify_features.py"
    echo -e "    Export CSV data : python3 data-collection/export_training_data.py"
    echo -e "    Check logs      : tail -f /tmp/ppa_watchdog.log"
    echo ""
fi