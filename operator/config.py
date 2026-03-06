# operator/config.py — cluster-wide defaults only
# App-specific values (target, model paths, scaling params) come from CRD spec.

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.constants import CAPACITY_PER_POD
from common.promql import RATE_WINDOW

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring:9090")
NAMESPACE = "default"
SCRAPE_WINDOW = RATE_WINDOW
LOOKBACK_STEPS = 12          # 12 operator samples x 30s timer = 6 minutes of live history
PREDICTION_HORIZON = 10      # predict 10 min ahead
STABILIZATION_STEPS = 2      # consecutive stable reads before acting
TIMER_INTERVAL = 30          # seconds
INITIAL_DELAY = 60           # seconds — wait for metrics warmup

DEFAULT_CAPACITY_PER_POD = CAPACITY_PER_POD
DEFAULT_MIN_REPLICAS = 2
DEFAULT_MAX_REPLICAS = 20
DEFAULT_SCALE_UP_RATE = 2.0
DEFAULT_SCALE_DOWN_RATE = 0.5
DEFAULT_MODEL_DIR = "/models"
