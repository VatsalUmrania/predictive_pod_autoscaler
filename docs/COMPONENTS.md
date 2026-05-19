---
title: Components Reference
description: API reference for all major components
last-updated: 2026-05-16
---

# Components Reference {#components}

This document catalogs all major components in the PPA and NEXUS systems with their purpose, key files, entry points, and configuration.

---

## PPA Components {#ppa-components}

### Operator {#operator}

**Purpose:** Kubernetes operator that reconciles `PredictiveAutoscaler` CRs and makes scaling decisions.

**Location:** `src/ppa/operator/main.py`

**Entry Points:**
```python
# src/ppa/operator/main.py:1428
@kopf.on.startup()
def startup(**kwargs):
    """Operator startup hook."""

@kopf.timer("ppa.example.com", "v1", "predictiveautoscalers", interval=30)
def reconcile(spec, status, meta, patch, **kwargs):
    """Main reconciliation loop (every 30s per CR)."""
```

**Key Functions:**
| Function | Line | Purpose |
|----------|------|---------|
| `_parse_crd_spec()` | 666 | Parse CR spec, resolve model paths |
| `_fetch_and_validate_features()` | 1013 | Query Prometheus, build feature vector |
| `_update_predictor_state()` | 1076 | Update predictor history |
| `_make_scaling_decision()` | 1145 | Predict load, calculate target replicas |
| `_apply_scaling()` | 1381 | Scale deployment (if non-observer) |

**Dependencies:**
- `kopf` - Kubernetes operator framework
- `prometheus_client` - Prometheus metrics SDK
- `tensorflow-lite` - TFLite runtime
- `sklearn` - Feature scaler

**Configuration:**
```python
# src/ppa/config.py:134-145
TIMER_INTERVAL = 30              # Reconciliation interval (seconds)
INITIAL_DELAY = 60               # Startup delay
STABILIZATION_STEPS = 2          # Cycles to stabilize
STABILIZATION_TOLERANCE = 0.5    # Replica tolerance
DEFAULT_MIN_REPLICAS = 2
DEFAULT_MAX_REPLICAS = 20
DEFAULT_SCALE_UP_RATE = 2.0
DEFAULT_SCALE_DOWN_RATE = 0.5
```

**See:** [PPA Architecture](PPA_ARCHITECTURE.md)

---

### Predictor {#predictor}

**Purpose:** TFLite inference engine with history tracking and concept drift detection.

**Location:** `src/ppa/operator/predictor.py`

**Entry Points:**
```python
class Predictor:
    def __init__(self, model_path, scaler_path, target_scaler_path=None, metadata_path=None, version=""):
        """Load TFLite model and sklearn scaler."""
    
    def predict(self) -> float:
        """Run inference on current history window."""
    
    def update(self, features: dict) -> None:
        """Add features to history buffer."""
    
    def ready(self) -> bool:
        """Return True if history window is full."""
```

**Key Methods:**
| Method | Purpose |
|--------|---------|
| `predict()` | Run TFLite inference, return predicted RPS |
| `update()` | Add step to rolling history (maxlen=60) |
| `track_prediction_accuracy()` | Track MAPE for drift detection |
| `check_concept_drift()` | Detect distribution shift |

**Configuration:**
- `history.maxlen = 60` (30 minutes at 30s interval)
- Drift threshold: 20% MAPE (configurable)

---

### Feature Engine {#feature-engine}

**Purpose:** Extract features from Prometheus queries.

**Location:** `src/ppa/operator/features.py`

**Entry Points:**
```python
def build_feature_vector(target, target_ns, min_r, max_r, container_name, prom_url, state):
    """
    Build 14-feature vector from Prometheus metrics.
    
    Features:
    - Queried: rps_per_replica, cpu_utilization_pct, memory_utilization_pct,
               latency_p95_ms, active_connections, error_rate, cpu_acceleration,
               rps_acceleration, replicas_normalized
    - Temporal: hour_sin, hour_cos, dow_sin, dow_cos, is_weekend
    """
```

**Dependencies:**
- Prometheus HTTP API
- Kubernetes API (for current replicas, pod info)

**See:** [[PPA_ARCHITECTURE.md#step-2-fetch-features]]

---

### Scaler {#scaler}

**Purpose:** Apply scaling decisions to Kubernetes Deployments.

**Location:** `src/ppa/operator/scaler.py`

**Entry Points:**
```python
def scale_deployment(deployment_name: str, replicas: int, namespace: str = "default") -> bool:
    """
    Patch Deployment with new replica count.
    
    Returns True on success, False on failure.
    Uses `kubectl patch` or K8s API directly.
    """
```

---

### Model Bundle {#model-bundle}

**Purpose:** Resolve and validate model artifact paths.

**Location:** `src/ppa/operator/model_bundle.py`

**Entry Points:**
```python
def resolve_model_bundle(model_dir: str, target_app: str, target_horizon: str) -> ModelBundle:
    """
    Resolve model artifacts from directory structure.
    
    Preferred (structured):
      /models/{app}/{horizon}/current/ppa_model.tflite
      /models/{app}/{horizon}/current/scaler.pkl
    
    Fallback (flat):
      /models/{horizon}/ppa_model.tflite
      /models/{horizon}/scaler.pkl
    """
```

---

### CLI {#ppa-cli}

**Purpose:** Command-line interface for PPA operations.

**Location:** `src/ppa/cli/`

**Structure:**
```
src/ppa/cli/
├── __main__.py      # Entry point
├── main.py          # CLI application
└── commands/        # Command implementations
```

**Commands:**
| Command | File | Purpose |
|---------|------|---------|
| `ppa operator build` | `cli/commands/operator.py` | Build operator Docker image |
| `ppa operator deploy` | `cli/commands/operator.py` | Deploy operator to K8s |
| `ppa operator status` | `cli/commands/operator.py` | Check operator status |
| `ppa operator logs` | `cli/commands/operator.py` | Stream operator logs |
| `ppa train` | `cli/commands/train.py` | Train custom model |
| `ppa add` | `cli/commands/add.py` | Add/annotate applications |
| `ppa push` | `cli/commands/push.py` | Publish metrics |

**See:** [[CLI_REFERENCE.md#ppa-commands]]

---

## NEXUS Components {#nexus-components}

### Base Agent {#base-agent}

**Purpose:** Abstract foundation for all domain agents.

**Location:** `src/nexus/agents/base_agent.py`

**Entry Points:**
```python
class BaseAgent(ABC):
    @abstractmethod
    async def sense() -> List[IncidentEvent]:
        """Observe domain and return incidents."""
    
    async def run() -> None:
        """Main poll loop with circuit breaker."""
```

**All Domain Agents:**
- `k8s_agent.py` - Kubernetes failures
- `metrics_agent.py` - SLO violations
- `config_agent.py` - Configuration errors
- `db_agent.py` - Database issues
- `nginx_agent.py` - HTTP errors
- `git_agent.py` - Git/deploy events
- `network_agent.py` - Network issues

**See:** [[NEXUS_ARCHITECTURE.md#healing-agents]]

---

### RCA Engine {#rca-engine}

**Purpose:** Root cause analysis with LLM + rule-based fallback.

**Location:** `src/nexus/reasoning/rca_engine.py`

**Entry Points:**
```python
class RCAEngine:
    async def analyze(self, cluster: IncidentCluster) -> RCAResult:
        """
        Analyze incident cluster and return structured result.
        
        Uses LLM (Gemini/OpenAI/Anthropic) with rule-based fallback.
        Returns:
        - root_cause: Human-readable explanation
        - failure_class: bad_deploy|resource_exhaustion|...
        - healing_level: 0-3
        - runbook_id: Suggested runbook
        - confidence: 0.0-1.0
        """
```

**Configuration:**
```python
NEXUS_GEMINI_API_KEY      # Gemini API key (optional)
NEXUS_GEMINI_MODEL        # Default: gemini-1.5-flash
NEXUS_RCA_TIMEOUT_S       # Timeout: 10 seconds
```

**See:** [[NEXUS_ARCHITECTURE.md#rca-engine]]

---

### Event Bus {#event-bus}

**Purpose:** NATS-based event routing.

**Location:** `src/nexus/bus/`

**Key Files:**
| File | Purpose |
|------|---------|
| `nats_client.py` | NATS connection management |
| `incident_event.py` | IncidentEvent dataclass |
| `event_correlator.py` | Event correlation logic |
| `router.py` | Event routing rules |

---

### Governance {#governance}

**Purpose:** Policy enforcement and action ladder.

**Location:** `src/nexus/governance/`

**Key Components:**
| Component | Purpose |
|-----------|---------|
| `policy_engine.py` | Evaluate action policies |
| `action_ladder.py` | Graduated response logic |
| `approval_gateway.py` | Approval workflow |

---

### SDK {#sdk}

**Purpose:** Client libraries for application integration.

**Location:** `src/nexus/sdk/`

**Languages:**
- Python (primary)
- JavaScript/TypeScript (planned)

**API:**
```python
from nexus_sdk import NexusClient

client = NexusClient(url="http://nexus-server")
client.publish_incident(...)
client.get_incident(id)
```

---

## Shared Components {#shared}

### Config {#config-module}

**Location:** `src/ppa/config.py`

Single source of truth for all configuration:

```python
from ppa.config import (
    get_config,
    PROMETHEUS_URL,
    DEFAULT_MODEL_DIR,
    FEATURE_COLUMNS,
    TIMER_INTERVAL,
)
```

---

### Domain {#domain}

**Location:** `src/ppa/domain/`

Domain models and business logic:

```python
from ppa.domain import CRState, calculate_replicas
```

---

## Prometheus Metrics Reference {#metrics-reference}

### PPA Metrics {#ppa-metrics}

| Metric | Type | Description |
|--------|------|-------------|
| `ppa_predicted_load_rps` | Gauge | LSTM predicted load |
| `ppa_inflated_load_rps` | Gauge | Predicted × safety factor |
| `ppa_desired_replicas` | Gauge | Target replicas |
| `ppa_current_replicas` | Gauge | Observed replicas |
| `ppa_scale_events_total` | Counter | Total scaling events |
| `ppa_circuit_breaker_tripped` | Gauge | Circuit breaker state |
| `ppa_concept_drift_detected` | Gauge | Drift detection flag |
| `ppa_inference_latency_ms` | Gauge | Model inference time |

**Endpoint:** `:9100/metrics`

### Operator Health {#operator-health}

| Endpoint | Port | Purpose |
|----------|------|---------|
| `/healthz` | 8080 | Liveness probe |
| `/metrics` | 9100 | Prometheus scraping |

---

## See Also {#see-also}

- [Architecture](ARCHITECTURE.md) - System overview
- [CLI Reference](CLI_REFERENCE.md) - Complete CLI documentation
- [Operations](OPERATIONS.md) - Operational procedures
