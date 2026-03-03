# operator/config.py — single source of truth
PROMETHEUS_URL      = "http://prometheus-kube-prometheus-prometheus.monitoring:9090"
TARGET_APP          = "test-app"
NAMESPACE           = "default"
SCRAPE_WINDOW       = "2m"
LOOKBACK_STEPS      = 12          # 12 × 5min = 60 min input window
PREDICTION_HORIZON  = 10          # predict 10 min ahead
CAPACITY_PER_POD    = 50          # req/s each pod can handle
MIN_REPLICAS        = 2
MAX_REPLICAS        = 20
SCALE_UP_RATE_LIMIT = 2.0         # max 2× current replicas per cycle
SCALE_DOWN_RATE     = 0.5         # max 50% reduction per cycle
STABILIZATION_STEPS = 2           # consecutive reads before acting
MODEL_PATH          = "/app/model/ppa_model.tflite"
SCALER_PATH         = "/app/model/scaler.pkl"
TIMER_INTERVAL      = 30          # seconds
INITIAL_DELAY       = 60          # seconds — wait for metrics warmup
