# operator/config.py — cluster-wide defaults only
# App-specific values (target, model paths, scaling params) come from CRD spec.

import os

PROMETHEUS_URL      = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring:9090")
NAMESPACE           = "default"
SCRAPE_WINDOW       = "2m"
LOOKBACK_STEPS      = 12          # 12 × 5min = 60 min input window
PREDICTION_HORIZON  = 10          # predict 10 min ahead
STABILIZATION_STEPS = 2           # consecutive stable reads before acting
TIMER_INTERVAL      = 30          # seconds
INITIAL_DELAY       = 60          # seconds — wait for metrics warmup

# Defaults used when CRD spec omits optional fields
DEFAULT_CAPACITY_PER_POD    = 50
DEFAULT_MIN_REPLICAS        = 2
DEFAULT_MAX_REPLICAS        = 20
DEFAULT_SCALE_UP_RATE       = 2.0
DEFAULT_SCALE_DOWN_RATE     = 0.5
DEFAULT_MODEL_DIR           = "/models"
