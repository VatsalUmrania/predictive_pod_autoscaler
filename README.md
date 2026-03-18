# Predictive Pod Autoscaler (PPA)

[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.28%2B-blue.svg)](https://kubernetes.io)
[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://python.org)
[![Prometheus](https://img.shields.io/badge/Prometheus-Monitoring-orange.svg)](https://prometheus.io)
[![TensorFlow Lite](https://img.shields.io/badge/TensorFlow-Lite-FF6F00.svg)](https://tensorflow.org/lite)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

> **Proactive, ML-driven Horizontal Pod Autoscaling for Kubernetes environments.**

Standard Kubernetes Horizontal Pod Autoscalers (HPAs) are inherently reactive; they scale infrastructure *after* a load threshold is breached, often resulting in dropped requests during the spin-up latency window. 

The **Predictive Pod Autoscaler (PPA)** solves this by utilizing deeply integrated Long Short-Term Memory (LSTM) neural networks. Operating entirely inside the cluster via the Kopf operator framework, PPA forecasts application request rates (RPS) 3 to 10 minutes into the future. By preemptively patching deployments, PPA guarantees capacity is available *before* the traffic spike arrives, delivering zero-downtime scaling.

---

##  Architecture Philosophy

PPA is designed with a strict separation of concerns, decoupling offline heavy lifting from critical-path online inference.

1. **Deterministic Data Engine:** A clustered CronJob coordinates with a specialized Locust traffic generator to synthesize highly volatile, multi-stage traffic patterns. This aggressively triggers standard HPA bounds, enabling the extraction of a high-variance, 14-dimensional feature set natively from Prometheus.
2. **Offline Training:** The raw time-series metrics are formatted into segment-aware sliding windows. A Keras LSTM model is trained locally to correlate cyclical momentum indicators with future state vectors.
3. **Edge Inference (TFLite):** To ensure cluster stability and sub-100ms prediction latency, the trained model is quantized and converted to `.tflite`. The operator mounts these models dynamically via persistent volumes, eliminating the architectural anti-pattern of burying ML artifacts inside Docker images or relying on external API cold-starts.
4. **Multi-Tenant Operator:** The custom `PredictiveAutoscaler` CRD allows a single, stateless operator to manage heterogeneous scaling policies and distinct models across independent namespaces simultaneously.

---

##  Quick Start

A comprehensive wrapper is provided to bootstrap the local development environment (requires `minikube`, `kubectl`, and `helm`). The Minikube driver is auto-detected based on your platform (`kvm2` on Linux, `docker` on macOS/Windows).

```bash
# 1. Boot the Minikube cluster, install the kube-prometheus-stack, 
# deploy the instrumented test application, and spin up the Locust swarm.
./ppa_startup.sh

# 2. Wait ~2 hours. The Locust swarm runs in FAST_MODE, aggressively
# compounding traffic to force mathematical variance in the metrics.

# 3. Export the 14-dimensional feature set to CSV via the virtual environment.
venv/bin/python data-collection/export_training_data.py --hours 168 --step 15s
```

---

##  Technical Documentation

Comprehensive documentation has been separated into the `docs/` index to maintain repository cleanliness.

| Category | Resource | Description |
| :--- | :--- | :--- |
|**System Design** | [Architecture Hub](./docs/architecture.md) | Macro topology & interaction diagrams |
| | [Data Collection](./docs/architecture/data_collection.md) | Metrics & chaotic load generation |
| | [ML Operator](./docs/architecture/ml_operator.md) | Kopf operator & TFLite inference |
|**Operations** | [Command Reference](./docs/reference/ppa_commands.md) | CLI reference for cluster debugging |
| | [Working Queries](./docs/reference/working_queries.md) | PromQL snippet libraries |
|**Historical** | [Archive](./docs/archive) | Specs, audits, and legacy records |

---