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
NAMESPACE = os.getenv("PPA_NAMESPACE", "default")
SCRAPE_WINDOW = RATE_WINDOW
LOOKBACK_STEPS = int(os.getenv("PPA_LOOKBACK_STEPS", "60"))  # 60 × 30s = 30 min history (must match training)
STABILIZATION_STEPS = int(os.getenv("PPA_STABILIZATION_STEPS", "2"))  # consecutive stable reads
STABILIZATION_TOLERANCE = float(os.getenv("PPA_STABILIZATION_TOLERANCE", "0.5"))  # ±replicas (PR#2: tolerance-based stabilization)
TIMER_INTERVAL = int(os.getenv("PPA_TIMER_INTERVAL", "30"))  # seconds
INITIAL_DELAY = int(os.getenv("PPA_INITIAL_DELAY", "60"))  # seconds — metrics warmup
PROM_FAILURE_THRESHOLD = int(os.getenv("PPA_PROM_FAILURE_THRESHOLD", "10"))  # escalate to ERROR

DEFAULT_CAPACITY_PER_POD = CAPACITY_PER_POD
DEFAULT_MIN_REPLICAS = 2
DEFAULT_MAX_REPLICAS = 20
DEFAULT_SCALE_UP_RATE = 2.0
DEFAULT_SCALE_DOWN_RATE = 0.5
DEFAULT_MODEL_DIR = "/models"


# Exception classes
class FeatureVectorException(Exception):
    """Raised when feature extraction fails (Prometheus unavailable, network issues, etc.)."""
    pass
