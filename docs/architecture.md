# Predictive Pod Autoscaler (PPA) — Macro Architecture

The PPA system is divided into two distinct, decoupled pipelines. This "Hub" document provides a 10,000-foot overview of how they interact.

For detailed documentation, please refer to the specific subsystems below:

## 1. [Data Collection & Load Generation](./architecture/data_collection.md)
**Focus:** Infrastructure, metrics scraping, and training data creation.

This pipeline is responsible for:
- Generating dynamic HTTP traffic spikes using Locust in `FAST_MODE`.
- Triggering the Kubernetes `HorizontalPodAutoscaler` to create replica variance.
- Running the `ppa-data-collector` CronJob hourly to extract 14 specialized features from Prometheus.
- Safely appending time-series data to the `training-data-pvc` for offline LSTM training.

👉 **[Read the Data Collection Architecture](./architecture/data_collection.md)**

---

## 2. [Operator & ML Pipeline](./architecture/ml_operator.md)
**Focus:** Keras training, TFLite inference, and active custom resource reconciliation.

This pipeline is responsible for:
- Formatting the raw CSVs into sliding windows and training the Keras LSTM model.
- Loading the resulting `.tflite` model into the online `ppa-operator`.
- Reconciling `PredictiveAutoscaler` Custom Resources (CRs) independently in real-time.
- Fetching live 15s PromQL metrics, generating a 12-step rolling window, inferencing the future RPS, and preemptively patching the deployment replicas.

👉 **[Read the Operator & ML Architecture](./architecture/ml_operator.md)**

---

## High-Level Topology

```mermaid
flowchart TD
    subgraph Data Generation (Cluster)
        locust[Locust Generator] -->|Scales Pods| testapp[test-app]
        testapp -.->|Metrics| prom[(Prometheus)]
    end

    subgraph Data Extraction (Cluster)
        cron[ppa-data-collector] -->|Extracts| prom
        cron -->|Writes CSV| csv[(training-data-pvc)]
    end

    subgraph ML Pipeline (Offline)
        csv -.->|Train| lstm[Keras LSTM]
        lstm -.->|Export| tflite[.tflite Model]
    end

    subgraph PPA Operator (Cluster)
        op[ppa-operator] -->|Loads| tflite
        op -->|Live Queries| prom
        op -->|Predicts & Scales| testapp
    end

    Data Generation ~~~ Data Extraction ~~~ ML Pipeline ~~~ PPA Operator
```
