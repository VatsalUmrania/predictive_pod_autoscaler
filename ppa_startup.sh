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
    echo "  5  — Build & deploy instrumented test-app"
    echo "  6  — Deploy staged Locust traffic generator"
    echo "  7  — Start port-forwards (Prometheus + Grafana)"
    echo "  8  — Start port-forward watchdog"
    echo "  9  — Verify all 14 ML features (including T+3m shift)"
    echo "  10 — Deploy Data Collection CronJob"
    echo "  11 — [Manual] Run Fixed-Replica Chaos Profiling"
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

kubectl create namespace monitoring 2>/dev/null || true
info "Applying PPA Dashboard ConfigMap..."
kubectl apply -f "$PROJECT_DIR/deploy/grafana-dashboard-configmap.yaml"

if helm status prometheus -n monitoring &>/dev/null; then
    log "Prometheus already installed — ensuring Grafana sidecar is enabled..."
    helm upgrade prometheus prometheus-community/kube-prometheus-stack \
        --namespace monitoring \
        --reuse-values \
        --set grafana.sidecar.dashboards.enabled=true \
        --set grafana.sidecar.dashboards.searchNamespace=monitoring \
        --timeout=5m
else
    helm install prometheus prometheus-community/kube-prometheus-stack \
        --namespace monitoring \
        --set grafana.adminPassword=admin123 \
        --set prometheus.prometheusSpec.retention=30d \
        --set prometheus.prometheusSpec.scrapeInterval=15s \
        --set grafana.sidecar.dashboards.enabled=true \
        --set grafana.sidecar.dashboards.searchNamespace=monitoring \
        --timeout=5m

    info "Waiting for Prometheus pods..."
    sleep 30
    wait_for_pods "app.kubernetes.io/name=prometheus" "monitoring"
    log "Prometheus stack installed"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 5 — Build & Deploy Instrumented Test App
# ═══════════════════════════════════════════════════════════════
if ! run_step 5; then
heading "STEP 5 — Building & Deploying Instrumented Test App"

# Build inside minikube's Docker daemon (subshell keeps DOCKER_* vars scoped)
info "Building test-app image inside minikube..."
(
    eval $(minikube docker-env)
    docker build -t test-app:latest "$PROJECT_DIR/data-collection/test-app/"
)
log "Docker image built: test-app:latest"

# Deploy app + service + PodMonitor + HPA
kubectl apply -f "$PROJECT_DIR/data-collection/test-app-deployment.yaml"
if kubectl get deployment test-app -n default &>/dev/null; then
    log "test-app updated — rolling restart with new image..."
    kubectl rollout restart deployment/test-app
fi

info "Waiting for test-app pods..."
sleep 10
wait_for_pods "app=test-app" "default"

# Verify single container (no sidecars)
CONTAINERS=$(kubectl get pods -l app=test-app -o jsonpath='{.items[0].status.containerStatuses}' 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))" 2>/dev/null || echo "?")
if [[ "$CONTAINERS" == "1" ]]; then
    log "Single container confirmed (1/1) — no sidecars"
else
    warn "Unexpected container count: $CONTAINERS — check manually"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 6 — Deploy In-Cluster Traffic Generator
# ═══════════════════════════════════════════════════════════════
if ! run_step 6; then
heading "STEP 6 — Deploying In-Cluster Traffic Generator"

kubectl create configmap traffic-gen-locustfile \
    --namespace default \
    --from-file=locustfile.py="$PROJECT_DIR/tests/locustfile.py" \
    --dry-run=client \
    -o yaml | kubectl apply -f -

kubectl apply -f "$PROJECT_DIR/deploy/traffic-gen-deployment.yaml"
kubectl rollout restart deployment/traffic-gen -n default &>/dev/null || true
wait_for_pods "app=traffic-gen" "default"
log "Staged Locust traffic generator running in-cluster"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 7 — Start Port Forwards
# ═══════════════════════════════════════════════════════════════
if ! run_step 7; then
heading "STEP 7 — Starting Port Forwards"

# Kill existing port-forwards
pkill -f "port-forward.*9090" 2>/dev/null || true
pkill -f "port-forward.*3000" 2>/dev/null || true
pkill -f "port-forward.*8080" 2>/dev/null || true
pkill -f "port-forward.*9091" 2>/dev/null || true
sleep 2

# Wait for Prometheus pod FIRST — this is the bottleneck
info "Waiting for Prometheus pod to be ready (may take 2-3 minutes)..."
for i in $(seq 1 36); do
    POD_STATUS=$(kubectl get pods -n monitoring -l app.kubernetes.io/name=prometheus --no-headers 2>/dev/null | awk '{print $2}')
    if [[ "$POD_STATUS" == "2/2" ]]; then
        log "Prometheus pod ready ($POD_STATUS)"
        break
    fi
    echo -n "  [$i/36] Pod status: ${POD_STATUS:-not found}..."
    echo ""
    sleep 10
done

# Start fresh port-forwards
kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &>/dev/null &
kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &>/dev/null &
kubectl port-forward svc/test-app 8080:80 -n default &>/dev/null &
kubectl port-forward svc/test-app 9091:9091 -n default &>/dev/null &
sleep 3

# Retry until Prometheus responds (port-forward may need a moment)
for i in $(seq 1 10); do
    if curl -s http://localhost:9090/-/ready 2>/dev/null | grep -q "Ready"; then
        log "Prometheus responding  → http://localhost:9090"
        log "Grafana port-forward   → http://localhost:3000 (admin/admin123)"
        log "test-app port-forward  → http://localhost:8080"
        log "test-app metrics       → http://localhost:9091/metrics"
        break
    fi
    if [[ $i -eq 10 ]]; then
        warn "Prometheus port-forward not responding after 30s — watchdog will retry"
    fi
    sleep 3
done
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 8 — Port Forward Watchdog
# ═══════════════════════════════════════════════════════════════
if ! run_step 8; then
heading "STEP 8 — Starting Port Forward Watchdog"

cat > /tmp/ppa_watchdog.sh << 'WATCHEOF'
#!/bin/bash
while true; do
    # 1. Prometheus
    if ! curl -s http://localhost:9090/-/ready > /dev/null 2>&1; then
        echo "$(date) — Restarting Prometheus port-forward..."
        pkill -f "port-forward.*9090" 2>/dev/null
        kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &>/dev/null &
    fi
    
    # 2. Grafana
    if ! curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
        echo "$(date) — Restarting Grafana port-forward..."
        pkill -f "port-forward.*3000" 2>/dev/null
        kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &>/dev/null &
    fi

    # 3. Test App (App Endpoint)
    if ! curl -s http://localhost:8080/ > /dev/null 2>&1; then
        echo "$(date) — Restarting Test App (8080) port-forward..."
        pkill -f "port-forward.*8080" 2>/dev/null
        kubectl port-forward svc/test-app 8080:80 -n default &>/dev/null &
    fi

    # 4. Test App (Metrics)
    if ! curl -s http://localhost:9091/metrics > /dev/null 2>&1; then
        echo "$(date) — Restarting Test App Metrics (9091) port-forward..."
        pkill -f "port-forward.*9091" 2>/dev/null
        kubectl port-forward svc/test-app 9091:9091 -n default &>/dev/null &
    fi

    sleep 30
done
WATCHEOF

chmod +x /tmp/ppa_watchdog.sh
nohup bash /tmp/ppa_watchdog.sh > /tmp/ppa_watchdog.log 2>&1 &
log "Watchdog running (PID: $!) — auto-restarts dead port-forwards every 30s"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 9 — Verify All 9 ML Features
# ═══════════════════════════════════════════════════════════════
if ! run_step 9; then
heading "STEP 9 — Verifying 14 ML Features (T+3m Shift Active)"

# Smart wait: retry until Prometheus is reachable, then wait for metrics
for i in $(seq 1 12); do
    if curl -s http://localhost:9090/-/ready 2>/dev/null | grep -q "Ready"; then
        info "Prometheus is ready — waiting 30s for metrics to populate..."
        sleep 30
        python3 "$PROJECT_DIR/data-collection/verify_features.py" || warn "Some features may still be warming up"
        break
    fi
    if [[ $i -eq 12 ]]; then
        warn "Prometheus not reachable after 2 minutes — run manually: python3 data-collection/verify_features.py"
    else
        echo "  [$i/12] Waiting for Prometheus..."
        sleep 10
    fi
done
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 10 — Export Training Data
# ═══════════════════════════════════════════════════════════════
if ! run_step 10; then
heading "STEP 10 — Deploying Data Collection CronJob"

cd "$PROJECT_DIR"
if curl -s http://localhost:9090/-/ready 2>/dev/null | grep -q "Ready"; then
    info "Building data collector image inside minikube..."
    # subshell keeps DOCKER_* vars scoped — prevents contaminating parent shell
    (
        eval $(minikube docker-env)
        docker build -f "$PROJECT_DIR/data-collection/Dockerfile" -t ppa-data-collector:latest "$PROJECT_DIR"
    )
    log "Collector image built: ppa-data-collector:latest"
    kubectl apply -f deploy/cronjob-data-collector.yaml
    log "CronJob created for hourly data collection"
else
    warn "Prometheus not reachable — skipping CronJob deployment. Run manually: kubectl apply -f deploy/cronjob-data-collector.yaml"
fi

# ═══════════════════════════════════════════════════════════════
#  STEP 11 — Fixed-Replica Chaos Profiling (Manual Option)
# ═══════════════════════════════════════════════════════════════
if ! run_step 11; then
heading "STEP 11 — Fixed-Replica Chaos Profiling"

info "This step scales the app and generates chaos load to profile capacity."
info "Running: ./scripts/fixed_replica_test.sh"
./scripts/fixed_replica_test.sh
log "Fixed-replica profiling run completed — #done session"
fi
fi

# ═══════════════════════════════════════════════════════════════
#  DONE
# ═══════════════════════════════════════════════════════════════
if [[ -z "$SINGLE_STEP" ]]; then
    echo ""
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${GREEN}║   PPA Data Collection Stack is Running!  ║${NC}"
    echo -e "${BOLD}${GREEN}║             #done session                ║${NC}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}Prometheus${NC}   → http://localhost:9090"
    echo -e "  ${CYAN}Grafana${NC}      → http://localhost:3000  (admin / admin123)"
    echo -e "  ${CYAN}Test App${NC}     → http://localhost:8080"
    echo ""
    echo "  ${YELLOW}Daily tasks:${NC}"
    echo -e "    Verify features : ${CYAN}python3 data-collection/verify_features.py${NC}"
    echo -e "    Manual Chaos    : ${CYAN}./scripts/fixed_replica_test.sh${NC}"
    echo -e "    Check data      : ${CYAN}tail -n 20 data-collection/training-data/training_data.csv${NC}"
    echo ""
    echo -e "${GREEN}#done session${NC}"
    echo ""
fi
