# Dockerfile Build Guide

**Predictive Pod Autoscaler | Image Optimization**

---

## Overview

The PPA project is split into three independent Docker images, each with its own minimal `requirements.txt`:

```
project/
├── requirements.txt          ← ROOT (for development & ML pipeline only)
├── operator/
│   ├── Dockerfile
│   └── requirements.txt      ← OPERATOR (minimal: kopf, kubernetes, requests)
├── data/
│   ├── Dockerfile
│   └── requirements.txt      ← DATA COLLECTION (metrics scraping)
└── model/
    └── (no Dockerfile — runs on laptop)
```

---

## Operator Image (Smallest)

### What's Included

```
kopf              — Kubernetes operator framework
kubernetes        — K8s API client
requests          — HTTP for Prometheus queries
numpy             — Numeric operations
joblib            — Load ML scalers from disk
```

**Size:** ~200-300 MB (slim Python 3.11 + minimal deps)

### Build for Minikube

```bash
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f operator/Dockerfile .
```

### Dockerfile Reference

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Use operator-specific requirements (not root requirements.txt)
COPY operator/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY operator/ .

CMD ["kopf", "run", "main.py", "--standalone"]
```

---

## Why Separate requirements.txt Files?

The root `requirements.txt` includes:
- **TensorFlow 2.20.0** (~500+ MB) — Only needed for `model/train.py`
- **Keras, scikit-learn** — Only needed for training
- **Pandas, matplotlib** — Only needed for evaluation
- **Flask** — Only needed for data collection
- **Locust** — Only needed for load testing

**The operator uses NONE of these at runtime.**

### Size Impact

| Setup | Image Size | Includes |
|-------|-----------|----------|
| **Old** (root requirements.txt) | ~2.5 GB | ML, operator, data collection, load testing |
| **New** (operator/requirements.txt) | ~250 MB | Only operator essentials |
| **Savings** | **90% reduction** | — |

---

## Build Commands

### Operator (What You'll Use)

```bash
# This is the only Minikube build command you need
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f operator/Dockerfile .

# Verify
docker images | grep ppa-operator
```

### Data Collection (Future)

```bash
# If you build the data collector container
eval $(minikube docker-env)
docker build -t ppa-data-collector:latest -f data/Dockerfile .
```

---

## Deployment

The operator deployment uses the optimized image:

```bash
./scripts/ppa_redeploy.sh --retrain --epochs 100
```

This will:
1. Build `ppa-operator:latest` with minimal deps
2. Apply CRD, RBAC, PVC
3. Load champion model onto PVC
4. Deploy operator pod with health probes
5. Apply PredictiveAutoscaler CR
6. Stream operator logs

---

## Summary

✅ **Operator image is now 90% smaller** by using operator-specific requirements

✅ **No performance impact** — same functionality, faster pulls and startup

✅ **Cleaner dependency management** — each component declares only what it needs

✅ **Easier to audit** — see exactly what each image includes
