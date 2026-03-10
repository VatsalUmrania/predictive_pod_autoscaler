# Operator Deployment Guide

**Step-by-step guide to deploy the PPA operator to your Kubernetes cluster**

---

## Prerequisites

**Kubernetes:**
- Kubernetes 1.24+ with CRD support
- `kubectl` CLI configured to access your cluster
- RBAC enabled (standard on most clusters)

**Storage:**
- PersistentVolumeClaim (PVC) for models at `/models`
- Recommended: 10 GB capacity

**Monitoring:**
- Prometheus 2.30+ running (separate stack or in-cluster)
- 15-second scrape interval for target applications
- Prometheus reachable via DNS (e.g., `prometheus.monitoring:9090`)

**ML Models:**
- Trained `.tflite` models for your target apps
- Per-app scaler files: `scaler.pkl` and `target_scaler.pkl`
- Models stored locally (will copy to PVC in step 3)

---

## Step 1: Create PersistentVolume & PersistentVolumeClaim

**Purpose:** Storage backend for ML models and scalers, shared between data collection and operator.

```bash
# 1. Create storage directory (if using local storage)
mkdir -p /mnt/models

# 2. Apply PVC manifest
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: PersistentVolume
metadata:
  name: models-pv
spec:
  capacity:
    storage: 10Gi
  accessModes:
    - ReadWriteOnce
  hostPath:
    path: /mnt/models
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: models-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  volumeName: models-pv
EOF

# 3. Verify
kubectl get pvc models-pvc
```

---

## Step 2: Create Custom Resource Definition (CRD)

**Purpose:** Defines the `PredictiveAutoscaler` Kubernetes object.

```bash
kubectl apply -f deploy/crd.yaml
```

**Verify:**
```bash
kubectl get crd predictiveautoscalers.ppa.example.com
```

**Expected output:**
```
NAME                                    CREATED AT
predictiveautoscalers.ppa.example.com   2026-03-10T10:00:00Z
```

---

## Step 3: Setup RBAC (Service Account & Roles)

**Purpose:** Grant operator pod permission to read CRs, patch deployments, and update status.

```bash
kubectl apply -f deploy/rbac.yaml
```

**Manifest creates:**
- `ServiceAccount` named `ppa-operator`
- `ClusterRole` with permissions to:
  - `get, list, watch, patch` on `PredictiveAutoscalers`
  - `patch` on `Deployments`
  - `get, list` on `Pods`
- `ClusterRoleBinding` connects them

**Verify:**
```bash
kubectl get sa ppa-operator
kubectl get clusterrole ppa-operator
kubectl get clusterrolebinding ppa-operator
```

---

## Step 4: Copy Trained Models to PVC

**Purpose:** Load pre-trained `.tflite` models and scaler files into the persistent storage.

### 4.1 Create directory structure

```bash
# First, ensure PVC is mounted and directories exist
kubectl run -it --rm --image=busybox --restart=Never -- \
  mkdir -p /mnt/models/test-app /mnt/models/other-app
```

### 4.2 Copy models from local filesystem

```bash
# Copy trained model files to PVC
# Format: kubectl cp <local_path> <pod>:<container_path>

# For test-app (5-minute horizon)
kubectl cp model/champions/rps_t5m/ppa_model.tflite \
  $(kubectl get pod -l volume=models-pvc -o jsonpath='{.items[0].metadata.name}'):/mnt/models/test-app/

kubectl cp model/champions/rps_t5m/scaler.pkl \
  $(kubectl get pod -l volume=models-pvc -o jsonpath='{.items[0].metadata.name}'):/mnt/models/test-app/

kubectl cp model/champions/rps_t5m/target_scaler.pkl \
  $(kubectl get pod -l volume=models-pvc -o jsonpath='{.items[0].metadata.name}'):/mnt/models/test-app/
```

### 4.3 Verify file transfer

```bash
kubectl run -it --rm --image=busybox --restart=Never -- \
  ls -lh /mnt/models/test-app/
```

**Expected output:**
```
total 5K
-rw-r--r-- 1 root root 114K Mar 10 10:00 ppa_model.tflite
-rw-r--r-- 1 root root 1.7K Mar 10 10:00 scaler.pkl
-rw-r--r-- 1 root root  719 Mar 10 10:00 target_scaler.pkl
```

---

## Step 5: Deploy Operator Pod

**Purpose:** Start the Kopf controller that watches CRs and reconciles autoscaling.

```bash
kubectl apply -f deploy/operator-deployment.yaml
```

**Manifest creates:**
- `Deployment` named `ppa-operator` with 1 replica
- Container image: `ppa-operator:latest` (must be built from `operator/Dockerfile`)
- Mounts PVC at `/models`
- Environment variables for Prometheus URL, reconciliation timer, etc.

**Verify pod is running:**
```bash
kubectl get pods -l app=ppa-operator -w

# Wait for READY 1/1, STATUS Running
```

**Check operator logs:**
```bash
kubectl logs -f deployment/ppa-operator
```

**Expected log output:**
```
[2026-03-10 10:00:00] kopf.reactor [INFO] Starting Kopf controller
[2026-03-10 10:00:01] ppa.operator [INFO] Initializing operator...
[2026-03-10 10:00:02] kopf.root [INFO] Operator started successfully
```

---

## Step 6: Create PredictiveAutoscaler Custom Resource

**Purpose:** Tell the operator which deployment to autoscale and with which model.

```bash
# Using example manifest
kubectl apply -f deploy/predictiveautoscaler.yaml
```

**Or create custom CR:**
```bash
cat <<EOF | kubectl apply -f -
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: test-app-ppa
  namespace: default
spec:
  targetDeployment: test-app              # Name of Deployment to autoscale
  namespace: default                       # Namespace containing the deployment
  modelPath: /models/test-app/ppa_model.tflite
  scalerPath: /models/test-app/scaler.pkl
  targetScalerPath: /models/test-app/target_scaler.pkl
  capacityPerPod: 80                      # RPS/pod at full capacity
  minReplicas: 2
  maxReplicas: 20
  scaleUpRate: 2.0                        # Max 2× per cycle up
  scaleDownRate: 0.5                      # Max 50% per cycle down
EOF
```

**Verify CR created:**
```bash
kubectl get ppa
kubectl describe ppa test-app-ppa
```

---

## Step 7: Monitor Operator

### 7.1 Watch CR status

```bash
kubectl get ppa -w

# Or more detailed:
kubectl get ppa test-app-ppa -o yaml | head -50
```

### 7.2 Follow operator logs

```bash
kubectl logs -f deployment/ppa-operator

# Or specific to your app:
kubectl logs -f deployment/ppa-operator | grep test-app-ppa
```

**Expected logs after ~2 minutes of warmup:**
```
[test-app-ppa] Warming up: 1/12 steps collected
[test-app-ppa] Warming up: 2/12 steps collected
...
[test-app-ppa] Warming up: 12/12 steps collected ✓
[test-app-ppa] RPS/Pod=15.2  P95=2340ms  CPU=5.1%  Replicas=2.5 (norm)
[test-app-ppa] Prediction: 1200 RPS -> 15 replicas (rate-limited: 2->5)
```

### 7.3 Check deployment replicas changing

```bash
kubectl get deployment test-app -w

# Or:
kubectl get deployment test-app -o jsonpath='{.status.replicas}' && echo " replicas"
```

---

## Step 8: Build Operator Docker Image

If the pre-built image isn't available, build it locally:

### 8.1 Build locally (host with Docker)

```bash
docker build -t ppa-operator:latest -f operator/Dockerfile .
```

### 8.2 Load into Minikube

```bash
# If using Minikube:
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f operator/Dockerfile .

# Verify:
docker images | grep ppa-operator
```

### 8.3 For remote Kubernetes (EKS, GKE, etc.)

```bash
# Build and push to registry
docker build -t <registry>/ppa-operator:latest -f operator/Dockerfile .
docker push <registry>/ppa-operator:latest

# Update deploy/operator-deployment.yaml:
#   image: <registry>/ppa-operator:latest

kubectl apply -f deploy/operator-deployment.yaml
```

---

## Troubleshooting Deployment

### Pod not starting: CrashLoopBackOff

```bash
# Check logs:
kubectl logs deployment/ppa-operator

# Common issues:
# - Prometheus not reachable (check PROMETHEUS_URL env var)
# - Model files not found in PVC
# - RBAC permissions missing
```

### CR status stuck "Warming up"

```bash
# Check if metrics are being collected:
kubectl logs deployment/ppa-operator | grep -E "Warming|metrics|error"

# Verify Prometheus is reachable:
kubectl run -it --rm --image=curlimages/curl --restart=Never -- \
  curl http://prometheus:9090/-/ready
```

### Models not accessible

```bash
# Verify PVC mounted:
kubectl describe pod $(kubectl get pod -l app=ppa-operator -o jsonpath='{.items[0].metadata.name}') | grep Mounts

# Check file permissions:
kubectl exec deployment/ppa-operator -- ls -la /models/test-app/
```

See **[Troubleshooting Guide](./troubleshooting.md)** for more solutions.

---

## Multi-App Setup

Deploy multiple PAs for different applications:

```bash
# App 1: test-app (5-min horizon)
kubectl apply -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: test-app-ppa
spec:
  targetDeployment: test-app
  modelPath: /models/test-app/ppa_model.tflite
  ... (other fields)
EOF

# App 2: api-server (3-min horizon, more aggressive scaling)
kubectl apply -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: api-server-ppa
spec:
  targetDeployment: api-server
  modelPath: /models/api-server/ppa_model.tflite
  scaleUpRate: 3.0
  ... (other fields)
EOF

# Verify both running:
kubectl get ppa
```

---

## Undeployment

To cleanly remove the operator:

```bash
# 1. Delete CRs (stops autoscaling)
kubectl delete ppa --all

# 2. Delete operator deployment
kubectl delete deployment ppa-operator

# 3. Delete RBAC
kubectl delete clusterrole ppa-operator
kubectl delete clusterrolebinding ppa-operator
kubectl delete sa ppa-operator

# 4. Optional: Delete CRD (removes PPA API entirely)
kubectl delete crd predictiveautoscalers.ppa.example.com

# 5. Optional: Delete PVC
kubectl delete pvc models-pvc
```

---

## Next Steps

- **[Configuration Reference](./configuration.md)** — Customize operator behavior
- **[API Reference](./api.md)** — Full CR specification
- **[Monitoring](./commands.md)** — Useful kubectl commands
- **[Troubleshooting](./troubleshooting.md)** — Debug common issues

