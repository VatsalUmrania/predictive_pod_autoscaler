# Predictive Pod Autoscaler Data Collection Architecture

## Overview
The data collection pipeline for the Predictive Pod Autoscaler (PPA) is a key component configured as a Kubernetes CronJob. It acts as an event-driven extractor, pulling detailed runtime metrics from Prometheus at defined intervals, pre-processing, and generating features suitable for LSTM model training and subsequent autoscaler operator predictions.

## Architecture

At a high level, the extraction process is stateless, self-contained, and configured to scrape via internal cluster DNS:

```mermaid
flowchart TD
    cronjob((CronJob\nppa-data-collector))
    config[config.py\nEnvironment Vars / Queries]
    prom[(Prometheus API\nkube-prometheus-stack)]
    pvc[(PersistentVolumeClaim\ntraining-data-pvc)]

    cronjob -->|runs hourly| extract[export_training_data.py]
    extract -->|loads config| config
    extract -.->|PromQL Queries| prom
    prom -.-> |JSON responses| extract
    extract -->|writes append| pvc
    
    subgraph Data Pipeline[Data Processing]
        extract --> df[build_feature_dataframe]
        df --> dataset[prepare_dataset\ncalculate derivative targets]
    end
```

## Features

An optimal 9-feature dimension is collected in line with the Phase 2 specification goals, structured into core load signals, state awareness features, unique indicators, momentum calculations, and generated cyclical signals. 

| Feature Category | Features |
| --- | --- |
| **Core Load** | `requests_per_second`, `cpu_usage_percent`, `memory_usage_bytes`, `latency_p95_ms` |
| **State** | `current_replicas` |
| **Indicators** | `active_connections`, `error_rate` |
| **Momentum** | `cpu_acceleration`, `rps_acceleration` |
| **Cyclical** | `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos`, `is_weekend` |

The target calculations generated dynamically from the state are `rps_t5`, `rps_t10`, `rps_t15` forecasting the target feature at time window advancements.

## Modules

The underlying stack is standard Python, utilizing `pandas` and `requests`. No sidecars or complex frameworks (e.g. Flask) are utilized inside this data-extractor pod. 

- `config.py`: Single source of truth. Contains core configurations dynamically parameterized (`TARGET_APP`, `PROMETHEUS_URL`) and the exhaustive map of `QUERIES` holding the raw robust PromQL patterns for calculation.
- `verify_features.py`: Independent troubleshooting script to locally poll against `PROMETHEUS_URL` and assert query matches data outputs, acting as a liveness probe on metrics readiness.
- `export_training_data.py`: Primary processor to load features via pandas dataframes dynamically over a stated window, evaluate targets natively through shifting constraints (`df.shift(-lag)`), safely append deduplicated blocks against long-running storage volumes, and format as `.csv`.
