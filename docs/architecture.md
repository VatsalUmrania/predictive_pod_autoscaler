# Predictive Pod Autoscaler (PPA) — Macro Architecture

The PPA system is divided into two distinct, decoupled pipelines. This "Hub" document provides a 10,000-foot overview of how they interact.

For detailed documentation, please refer to the specific subsystems below:

## 1. [Data Collection & Load Generation](./architecture/data_collection.md)
**Focus:** Infrastructure, metrics scraping, and training data creation.

This pipeline is responsible for:
- Generating dynamic chaotic HTTP traffic spikes using Locust `ChaoticLoadShape`.
- Collecting data across fixed scale bounds to construct non-linear capacity curves.
- Triggering the Kubernetes `HorizontalPodAutoscaler` to generate replica variance.
- Running the `ppa-data-collector` CronJob hourly to extract specialized features from Prometheus.
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
    subgraph DataGeneration ["Data Generation (Cluster)"]
        direction TB
        L1[Locust: ChaoticLoadShape] -->|Phased Spikes| APP[test-app]
        L2[Fixed-Replica Profiler] -->|Capacity Bounds| APP
        APP -.->|Metrics| PROM[(Prometheus)]
    end

    subgraph DataExtraction ["Data Extraction (Cluster)"]
        cron[ppa-data-collector] -->|T+3m Shift| PROM
        cron -->|Writes 14 Features| CSV[(training-data-pvc)]
    end

    subgraph MLPipeline ["ML Pipeline (Offline)"]
        CSV -.->|Train| LSTM[Keras LSTM]
        LSTM -.->|Quantize| TFLITE[.tflite Model]
    end

    subgraph PPAOperator ["PPA Operator (Cluster)"]
        OP[ppa-operator] -->|Dynamic Load| TFLITE
        OP -->|Live Inference| PROM
        OP -->|Preemptive Scale| APP
    end

    DataGeneration ~~~ DataExtraction ~~~ MLPipeline ~~~ PPAOperator
```
