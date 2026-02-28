# Working Prometheus Queries — Verified 2026-02-28

## All 8 LSTM Input Features

| Feature | Query |
|---|---|
| requests_per_second | `rate(istio_requests_total{destination_service=~"test-app.*"}[1m])` |
| cpu_usage_percent | `sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))*100` |
| memory_usage_bytes | `sum(container_memory_working_set_bytes{pod=~"test-app.*"})` |
| latency_p95_ms | `histogram_quantile(0.95, rate(istio_request_duration_milliseconds_bucket{destination_service=~"test-app.*"}[5m]))` |
| active_connections | `envoy_server_total_connections{pod=~"test-app.*"}` |
| error_rate | `sum(istio_requests_total{destination_service=~"test-app.*"})` |
| hour_of_day | Generated from timestamp in Python |
| day_of_week | Generated from timestamp in Python |

## Setup That Works
- Minikube: KVM2 driver
- Istio: 1.29.0 with demo profile
- Prometheus: kube-prometheus-stack
- Traffic: in-cluster traffic-gen deployment (NOT kubectl port-forward)
- Scrape config: additionalScrapeConfigs secret targeting port 15020

## Key Lessons Learned
- port-forward traffic bypasses Istio mesh — use in-cluster traffic-gen
- PodMonitor relabeling drops targets — use additionalScrapeConfigs instead
- cAdvisor scrapes without container labels — use sum() without container filter
- Istio 1.29 needs explicit Telemetry resource to enable prometheus metrics
