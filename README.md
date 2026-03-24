<div align="center">
  <h1>predictive-pod-autoscaler (PPA)</h1>
  <p><strong>Proactive, ML-Driven Horizontal Pod Autoscaling for Kubernetes</strong></p>

  [![Kubernetes](https://img.shields.io/badge/Kubernetes-1.28%2B-blue.svg?logo=kubernetes)](https://kubernetes.io)
  [![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python)](https://python.org)
  [![Prometheus](https://img.shields.io/badge/Prometheus-Monitoring-orange.svg?logo=prometheus)](https://prometheus.io)
  [![TensorFlow Lite](https://img.shields.io/badge/TensorFlow-Lite-FF6F00.svg?logo=tensorflow)](https://tensorflow.org/lite)
  [![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
</div>

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/list.svg" width="24" height="24" align="top"> Quick Links

- [What is PPA?](#-what-is-ppa) — The problem it solves
- [Get Started in 5 Minutes](#-get-started-in-5-minutes) — Deploy your first policy
- [How-To Guides](#-how-to-guides) — Common tasks & recipes
- [Reference](#-reference) — Configuration & API docs
- [Documentation & Architecture](#-full-documentation) — Deep dives
- [Contributing](#-contributing) — Join us

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/lightbulb.svg" width="24" height="24" align="top"> What is PPA?

**Predictive Pod Autoscaler (PPA)** is a Kubernetes operator that **scales your applications _before_ traffic spikes hit**, not after.

### The Problem
Traditional Horizontal Pod Autoscalers (HPAs) are **reactive**: they measure current load, see it's high, and spin up new pods. By then, you've already dropped requests and degraded performance.

```
Time:    0s           30s          60s           90s
Load:    ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▂▃▄▅▆▇█████████████
HPA:     ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▂▃▄▅▆▇████  ← Too late!
PPA:     ▁▁▁▁▁▁▁▁▁▁▁▁▂▃▄▅▆▇███████████████████  ← Ready in advance
```

### The Solution
PPA uses **LSTM neural networks** trained on your application's historical traffic patterns to forecast 3–10 minutes into the future. It scales **proactively**, ensuring capacity is ready when demand arrives.

**Result:** Zero-downtime scaling, lower latency, better user experience.

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/rocket.svg" width="24" height="24" align="top"> Get Started in 10 Minutes

### Prerequisites
```
✓ Kubernetes 1.28+
✓ Python 3.10+
✓ Prometheus already running (for metrics collection)
✓ Pre-trained ML models (included in repo)
```

> **Note:** If you don't have Prometheus, see [Setting Up Monitoring](./docs/operator/deployment.md#setup-prometheus) first.

### Step 1: Install the CLI
```bash
git clone https://github.com/VatsalUmrania/predictive_pod_autoscaler.git
cd predictive-pod-autoscaler

pip install -e .
```

### Step 2: Deploy the Operator
```bash
ppa operator build    # Build the operator Docker image
ppa operator deploy   # Deploy to your cluster
ppa operator status   # Verify it's running
```

### Step 3: Enable Predictive Scaling
Create a `PredictiveAutoscaler` policy for your deployment:

```yaml
apiVersion: autoscaling.ppa.io/v1alpha1
kind: PredictiveAutoscaler
metadata:
  name: my-app-autoscaling
  namespace: production
spec:
  targetRef:
    apiGroup: apps
    kind: Deployment
    name: my-app
  modelId: "lstm-rps-v2"       # Forecast model to use (pre-built)
  minReplicas: 2
  maxReplicas: 50
  lookAheadMinutes: 5          # Scale 5 minutes ahead
```

```bash
kubectl apply -f ppa-policy.yaml
```

**Done!** PPA now forecasts traffic for `my-app` and scales proactively. Monitor with:
```bash
kubectl get predictiveautoscaler -n production
ppa operator logs                             # Stream operator logs
```

> **Need a custom model?** See [Training a Custom Model](./docs/architecture/ml_pipeline.md#training-your-own-model).

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/book.svg" width="24" height="24" align="top"> How-To Guides

**I want to...**

### Deploy PPA to my cluster
Start with [Get Started](#-get-started-in-5-minutes) above, then see [Deployment Guide](./docs/operator/deployment.md).

### Configure scaling policies for my apps
See [Configuration Reference](./docs/operator/configuration.md) for full `PredictiveAutoscaler` CRD options.

### Integrate PPA with my existing HPA
Read [Coexisting with HPA](./docs/operator/README.md#coexisting-with-hpa) — PPA can work alongside standard HPAs.

### Debug scaling decisions
Use `ppa operator logs` to inspect the operator. See [Troubleshooting Guide](./docs/operator/troubleshooting.md).

### Train a custom ML model
PPA uses pre-built models, but you can train your own. See [ML Pipeline Guide](./docs/architecture/ml_pipeline.md).

### Monitor PPA's performance
PPA exports Prometheus metrics. Import the [Grafana Dashboard](./deploy/grafana-dashboard-configmap.yaml) for visualization.

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/sliders.svg" width="24" height="24" align="top"> Reference

### Environment Variables
Controls how the operator behaves at runtime.

| Variable | Description | Default |
|----------|-------------|---------|
| `PPA_MODEL_PATH` | Directory containing `.tflite` models | `/models` |
| `PPA_PROMETHEUS_URL` | Prometheus service URL (in-cluster) | `http://prometheus:9090` |
| `PPA_LOG_LEVEL` | Logging verbosity: `INFO`, `DEBUG`, `WARN` | `INFO` |
| `PPA_INFERENCE_TIMEOUT_MS` | Max time (ms) to wait for ML prediction | `100` |

### PredictiveAutoscaler CRD

The core Custom Resource that attaches predictive scaling to a Deployment.

```yaml
apiVersion: autoscaling.ppa.io/v1alpha1
kind: PredictiveAutoscaler
metadata:
  name: <name>
  namespace: <namespace>
spec:
  # Target application
  targetRef:
    apiGroup: apps
    kind: Deployment
    name: <deployment-name>
  
  # ML model
  modelId: "lstm-rps-v2"                    # Identifier of model to use
  
  # Scaling bounds
  minReplicas: 2
  maxReplicas: 50
  
  # Forecast horizon (in minutes)
  lookAheadMinutes: 5                       # Default: 5
  
  # Optional: sync with existing HPA
  syncWithHPA: true                         # Default: false
```

See the [API Reference](./docs/operator/api.md) for all options.

### Key Metrics & Signals

PPA extracts 14 features from Prometheus metrics for ML predictions:

| Signal | Purpose |
|--------|--------|
| Request Rate (RPS) | Requests per second and trends |
| Latency Percentiles | P50, P95, P99 response times |
| Error Rates | Failed request rates |
| Resource Utilization | CPU, memory per pod |
| Temporal Patterns | Hour-of-day, day-of-week signals |

For detailed PromQL snippets and how to validate metrics: [PromQL Reference](./docs/reference/working_queries.md)

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/cpu.svg" width="24" height="24" align="top"> Architecture & Deep Dives

### Why PPA Works

PPA enforces a strict separation between **offline training** and **online inference**:

1. **Data Collection (Offline)**
   - A Kubernetes CronJob runs traffic generators (Locust) to synthetically load your apps
   - Raw metrics flow into Prometheus in real-time
   - Historical data accumulates over days/weeks

2. **ML Training (Offline)**
   - Data is formatted into sliding windows (14 features, 7-day lookback)
   - LSTM neural networks learn patterns: "When I see _this_ traffic signature, demand spikes in 5–10 minutes"
   - Models are quantized to TensorFlow Lite (`.tflite`) for speed & size

3. **Prediction & Scaling (Online, <100ms)**
   - Kopf operator periodically queries Prometheus for current metrics
   - TFLite model infers future RPS (Requests Per Second)
   - Operator patches Deployment replicas pre-emptively
   - No heavy ML libraries on the critical path—pure binary inference

### Package Structure
```
src/ppa/
├── common/          # Shared interfaces, feature specs, PromQL queries
├── operator/        # Kopf operator: main loops, feature extraction, scaling
├── model/           # ML training, evaluation, TFLite conversion
├── dataflow/        # Traffic generation, metric collection, export
└── cli/             # Developer CLI for operator lifecycle management
```

### Learn More
- [Full Architecture Diagram & Overview](./docs/architecture.md)
- [Data Collection & Traffic Generation](./docs/architecture/data_collection.md)
- [Operator Design & Async Patterns](./docs/architecture/ml_operator.md)
- [ML Training Pipeline](./docs/architecture/ml_pipeline.md)
- [Production Roadmap & Future Vision](./docs/production_roadmap.md)

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/book-open.svg" width="24" height="24" align="top"> Full Documentation

Comprehensive guides and references for every aspect of PPA.

| Category | Resources |
|----------|-----------|
| **Getting Started** | [Quick Start](#-get-started-in-5-minutes) · [How-To Guides](#-how-to-guides) |
| **Operations** | [Deployment](./docs/operator/deployment.md) · [Configuration](./docs/operator/configuration.md) · [Troubleshooting](./docs/operator/troubleshooting.md) · [Commands](./docs/operator/commands.md) |
| **Architecture** | [Overview](./docs/architecture.md) · [ML Operator](./docs/architecture/ml_operator.md) · [Data Collection](./docs/architecture/data_collection.md) · [ML Pipeline](./docs/architecture/ml_pipeline.md) |
| **Reference** | [CRD API](./docs/operator/api.md) · [PromQL Queries](./docs/reference/working_queries.md) · [CLI Commands](./docs/reference/ppa_commands.md) |
| **Development** | [Dev Setup](./docs/DEVELOPMENT.md) · [Running Tests](./docs/DEVELOPMENT.md#testing) |

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/users.svg" width="24" height="24" align="top"> Contributing

We welcome contributions! Whether it's a bug report, feature request, or code contribution, please participate.

### Before You Start
1. Read [DEVELOPMENT.md](./docs/DEVELOPMENT.md) to set up your environment
2. Check out the [Architecture Guide](./docs/architecture.md) to understand the codebase
3. Look at [open issues](https://github.com/VatsalUmrania/predictive_pod_autoscaler/issues) for areas to contribute

### Submitting a PR
1. Fork the repository and create a feature branch
2. Make your changes and write tests: `pytest tests/`
3. Ensure code quality: `python -m pylint src/` 
4. Submit a PR with a clear description of what changed and why

---

## <img src="https://cdn.jsdelivr.net/npm/lucide-static@0.321.0/icons/file-text.svg" width="24" height="24" align="top"> License

PPA is open-source software licensed under the [MIT License](./LICENSE).