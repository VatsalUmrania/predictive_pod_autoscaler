# Working Prometheus Queries — Updated 2026-03-01

## 9-Feature LSTM Input Vector

| # | Feature | Query | Source |
|---|---|---|---|
| 1 | rps_per_replica | `sum(rate(http_requests_total{pod=~"test-app.*"}[1m])) / kube_deployment_status_replicas_ready{deployment="test-app",namespace="default"}` | app.py |
| 2 | latency_p95_ms | `histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{pod=~"test-app.*"}[5m])) by (le)) * 1000` | app.py |
| 3 | cpu_utilization_pct | `sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m])) / sum(kube_pod_container_resource_limits{resource="cpu", pod=~"test-app.*"}) * 100` | cAdvisor |
| 4 | memory_utilization_pct | `sum(container_memory_working_set_bytes{pod=~"test-app.*"}) / sum(kube_pod_container_resource_limits{resource="memory", pod=~"test-app.*"}) * 100` | cAdvisor |
| 5 | hour_sin | `sin(2π × hour / 24)` | Generated |
| 6 | hour_cos | `cos(2π × hour / 24)` | Generated |
| 7 | dow_sin | `sin(2π × dow / 7)` | Generated |
| 8 | dow_cos | `cos(2π × dow / 7)` | Generated |
| 9 | replicas_normalized | `kube_deployment_status_replicas_ready{deployment="test-app"} / MAX_REPLICAS` | kube-state-metrics |

## Feature Design Rationale

- **Column order**: primary signal first (RPS, latency), temporal context mid, state context last — LSTM attention prioritizes earlier features
- **Cyclical encoding**: sin/cos avoids false discontinuity at midnight (hour 23→0) and Sunday→Monday
- **replicas_normalized**: allows model to distinguish "high CPU due to under-provisioning" vs "high CPU due to genuine demand"
- **latency_p95_ms**: leading indicator — latency spikes before CPU because the queue fills up first

## Setup That Works
- Minikube: Auto-detected driver (kvm2 on Linux, docker on macOS/Windows)
- Prometheus: kube-prometheus-stack (PodMonitor auto-discovery)
- Test App: Custom Python app with prometheus_client (single container, metrics on :9091)
- Traffic: in-cluster traffic-gen deployment + Locust variable pattern
