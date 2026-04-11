# PPA Model Upload/Promotion System - Architecture & Issues

## Executive Summary

The Predictive Pod Autoscaler (PPA) has a **multi-stage model promotion system** where trained models flow from a development environment to a Kubernetes operator via a **Persistent Volume Claim (PVC)**. However, there are **critical gaps in the TFLite runtime dependency and model path configuration** that cause the "No TFLite runtime found" error.

---

## 1. How Models are Supposed to Flow

### Complete Pipeline: Train → Convert → Promote → Deploy → Operator Loads

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         DEVELOPMENT/LOCAL MACHINE                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Step 1: ppa model train                                                   │
│  ├─ Trains LSTM in Keras format                                            │
│  ├─ Saves: ppa_model_{horizon}.keras                                       │
│  ├─ Saves: scaler_{horizon}.pkl                                            │
│  └─ Saves: target_scaler_{horizon}.pkl                                     │
│                                                                             │
│  Step 2: ppa model convert                                                 │
│  ├─ Loads Keras model                                                      │
│  ├─ Applies int8 quantization                                              │
│  ├─ Saves: ppa_model_{horizon}.tflite                                      │
│  └─ Generates: ppa_model_{horizon}_metadata.json (schema validation)       │
│                                                                             │
│  Step 3: ppa model push                                                    │
│  ├─ Validates cluster connection                                           │
│  ├─ Creates loader pod in Kubernetes                                       │
│  ├─ Copies artifacts to pod via kubectl cp:                                │
│  │   ├─ ppa_model_{horizon}.tflite                                         │
│  │   ├─ ppa_model_metadata.json                                            │
│  │   └─ training_data.csv                                                  │
│  ├─ Runs regenerate_scalers.py INSIDE pod                                  │
│  │   └─ Regenerates .pkl files for pickle compatibility                    │
│  └─ Copies regenerated scalers back to local champion dir                  │
│                                                                             │
│  Artifacts stored in: data/champions/{app_name}/{namespace}/{horizon}/    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PERSISTENT VOLUME CLAIM (PVC)                           │
│                        ppa-models (/models)                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Models copied FROM champion dir TO PVC by push command:                   │
│  /models/{app_name}/{horizon}/                                             │
│  ├─ ppa_model.tflite                                                       │
│  ├─ scaler.pkl                                                             │
│  ├─ target_scaler.pkl                                                      │
│  └─ ppa_model_metadata.json                                                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    ↓
┌─────────────────────────────────────────────────────────────────────────────┐
│                 KUBERNETES OPERATOR POD (ppa-operator)                      │
│          Mounts PVC at /models (readOnly) + Loads Models at Runtime        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  CRD Spec (PredictiveAutoscaler) specifies:                                │
│  ├─ modelPath: "/models/{app}/{horizon}/ppa_model.tflite"                 │
│  ├─ scalerPath: "/models/{app}/{horizon}/scaler.pkl"                      │
│  └─ targetScalerPath: "/models/{app}/{horizon}/target_scaler.pkl"         │
│                                                                             │
│  Operator loads at runtime:                                                │
│  ├─ Predictor(model_path, scaler_path, target_scaler_path)                │
│  ├─ Reads metadata JSON for schema validation (PR#7, PR#8)                 │
│  ├─ Tries multiple TFLite runtimes in order:                               │
│  │   1. ai_edge_litert (LiteRT - preferred)                                │
│  │   2. tensorflow.lite (full TensorFlow)                                  │
│  │   3. tflite_runtime (lightweight runtime)                               │
│  └─ If all fail → "No TFLite runtime found" error                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Where Model Files are Stored and Referenced

### Storage Locations (Three Stages)

| Stage | Location | Purpose | Ownership |
|-------|----------|---------|-----------|
| **Development** | `data/artifacts/{app}/{namespace}/{horizon}/` | Training output | Local machine |
| **Promotion** | `data/champions/{app}/{namespace}/{horizon}/` | Canonical models before upload | Local machine |
| **Runtime** | `/models/{app}/{horizon}/` (on PVC in K8s) | Operator reads from here | Kubernetes |

### File Naming Convention

```
ppa_model_{horizon}.keras          → Keras model (full precision)
ppa_model_{horizon}.tflite         → TFLite model (quantized, prod-ready)
ppa_model_{horizon}_metadata.json  → Schema + quantization info
scaler_{horizon}.pkl               → Feature scaler (MinMaxScaler)
target_scaler_{horizon}.pkl        → Target RPS scaler (MinMaxScaler)
```

### CRD Specification (How Operator Finds Models)

**File:** `deploy/crd.yaml` + `deploy/operator-deployment.yaml`

```yaml
apiVersion: ppa.io/v1
kind: PredictiveAutoscaler
metadata:
  name: test-app-ppa
  namespace: default
spec:
  targetDeployment: test-app
  appName: test-app
  horizon: rps_t3m
  modelPath: "/models/test-app/rps_t3m/ppa_model.tflite"      # ← Override if needed
  scalerPath: "/models/test-app/rps_t3m/scaler.pkl"            # ← Override if needed
  targetScalerPath: "/models/test-app/rps_t3m/target_scaler.pkl"
```

**Default Path Convention (if modelPath not specified):**
```python
# src/ppa/operator/main.py:113-128 (_resolve_paths function)
DEFAULT_MODEL_DIR = "/models"  # From config.py

model_path = spec.get("modelPath") or os.path.join(
    DEFAULT_MODEL_DIR, target_app, target_horizon, "ppa_model.tflite"
)
# Result: /models/{app_name}/{horizon}/ppa_model.tflite

scaler_path = spec.get("scalerPath") or os.path.join(
    DEFAULT_MODEL_DIR, target_app, target_horizon, "scaler.pkl"
)
# Result: /models/{app_name}/{horizon}/scaler.pkl
```

### Path Resolution Logic (Operator Initialization)

**Code:** `src/ppa/operator/main.py:_parse_crd_spec()`

1. Reads CRD spec for optional `modelPath`, `scalerPath`, `targetScalerPath`
2. Falls back to convention: `/models/{app_name}/{horizon}/{file}`
3. Passes paths to `Predictor` class (predictor.py)
4. Predictor loads immediately (PR#10: exponential backoff on failure)

---

## 3. Model Loading Code and "No TFLite Runtime Found" Error

### The Critical Problem

**File:** `src/ppa/operator/predictor.py:121-169`

```python
def _try_load(self):
    """Attempt to load model, scaler, and target scaler. Idempotent with exponential backoff."""
    
    # Try lightweight LiteRT first, then tensorflow.lite, then tflite_runtime
    interpreter_loaded = False
    for loader_name, loader_fn in [
        ("ai_edge_litert", lambda: _load_ai_edge_litert_interpreter()),
        ("tensorflow.lite", lambda: __import__("tensorflow").lite.Interpreter),
        ("tflite_runtime", lambda: (
            __import__("tflite_runtime.interpreter", fromlist=["Interpreter"]).Interpreter
        )),
    ]:
        try:
            interpreter_class = loader_fn()
            self.interpreter = interpreter_class(model_path=self.model_path)
            self.interpreter.allocate_tensors()
            logger.info(f"Model loaded via {loader_name}")
            interpreter_loaded = True
            break
        except Exception as exc:
            logger.debug(f"{loader_name} failed: {exc}")
            continue

    if not interpreter_loaded:
        raise RuntimeError(
            "No TFLite runtime found (tried ai_edge_litert, tensorflow.lite, tflite_runtime)"
        )
```

### Root Causes

| # | Issue | Impact | Why It Happens |
|----|--------|--------|----------------|
| **1** | `ai_edge_litert==2.1.3` in `requirements.operator.txt` | Should work (first attempt) | Binary wheel unavailable for some platforms (ARM64, specific libc versions) |
| **2** | TensorFlow NOT in `requirements.operator.txt` | tensorflow.lite attempt fails | Kept minimal to reduce operator image size (512MB limit) |
| **3** | `tflite_runtime` NOT installed | Third fallback unavailable | Not in requirements at all |
| **4** | Operator Dockerfile minimal | No runtime installed as backup | See below |

### Operator Dockerfile (src/ppa/operator/Dockerfile)

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.operator.txt .

RUN pip install --no-cache-dir -r requirements.operator.txt && rm requirements.operator.txt

COPY src/ppa/ ./ppa/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import kopf; import sys; sys.exit(0)" || exit 1

CMD ["kopf", "run", "ppa/operator/main.py", "--standalone"]
```

**Problem:** Only installs what's in `requirements.operator.txt`:
```
kopf==1.43.0
kubernetes==35.0.0
prometheus-client==0.23.1
prometheus-api-client==0.7.0
requests==2.32.5
ai-edge-litert==2.1.3          # ← Only TFLite option
numpy==1.26.0
scikit-learn==1.3.2
PyYAML==6.0.3
python-dotenv==1.2.1
rich==14.3.3
typer==0.15.2
python-json-logger==4.0.0
```

**Missing Dependencies:**
- ❌ `tensorflow` or `tensorflow-lite` (for tensorflow.lite.Interpreter)
- ❌ `tflite-runtime` (for tflite_runtime.interpreter.Interpreter)
- ❌ Alternative TFLite runtimes

### Error Flow

```
Operator pod starts
    ↓
kopf loads ppa/operator/main.py
    ↓
PredictiveAutoscaler CR created
    ↓
_parse_crd_spec() calls _get_or_create_state()
    ↓
Predictor.__init__() calls _try_load()
    ↓
Tries: ai_edge_litert ──→ Import error or platform issue
    ↓
Tries: tensorflow.lite ──→ "ModuleNotFoundError: No module named 'tensorflow'"
    ↓
Tries: tflite_runtime ──→ "ModuleNotFoundError: No module named 'tflite_runtime'"
    ↓
Raises: RuntimeError("No TFLite runtime found...")
    ↓
Predictor._load_failed = True
Predictor.interpreter = None
    ↓
Predictor.ready() always returns False
    ↓
Operator enters DEGRADED MODE:
  - Metrics show: ppa_model_load_failed = 1
  - Scaling decisions default to: predicted_rps = 0.0
  - After 10 retries with exponential backoff (5 min): gives up
```

### Validation Logic (Schema Checking)

**Code:** `src/ppa/operator/predictor.py:62-119`

Assuming model loads, Predictor validates metadata:

```python
def _load_and_validate_metadata(self):
    """Load and validate model metadata to prevent schema mismatches.
    
    CRITICAL ERRORS (re-raised immediately):
    - Feature column mismatch: model trained with different features
    - JSON parse error: metadata corrupted
    
    WARNINGS (logged but don't fail):
    - Lookback mismatch: different history length
    - High quantization loss: >5% accuracy degradation
    - Missing metadata file: proceed without validation
    """
    model_dir = Path(self.model_path).parent
    metadata_path = model_dir / f"{Path(self.model_path).stem}_metadata.json"
    
    # Checks:
    # 1. metadata["feature_columns"] == FEATURE_COLUMNS (CRITICAL)
    # 2. metadata["lookback"] vs LOOKBACK_STEPS (WARNING)
    # 3. metadata["accuracy_loss_pct"] > 5.0 (WARNING)
```

---

## 4. Current Deployment and Model Promotion Workflow

### Step-by-Step Workflow (With Issues)

#### Phase 1: Local Training & Promotion

```bash
# 1. Train LSTM
$ ppa model train --csv data/training-data/training_data_v2.csv --target rps_t3m

# Outputs to: data/artifacts/test-app/default/rps_t3m/
├─ ppa_model_rps_t3m.keras
├─ ppa_model_rps_t3m_metadata.json
├─ scaler_rps_t3m.pkl
└─ target_scaler_rps_t3m.pkl

# 2. Evaluate model quality
$ ppa model evaluate --model data/artifacts/.../ppa_model_rps_t3m.keras \
    --scaler data/artifacts/.../scaler_rps_t3m.pkl \
    --csv data/training-data/training_data_v2.csv

# 3. Convert to TFLite
$ ppa model convert --app-name test-app --namespace default --target rps_t3m

# Outputs: data/artifacts/test-app/default/rps_t3m/
├─ ppa_model_rps_t3m.tflite (quantized, ~50KB)
└─ ppa_model_rps_t3m_metadata.json (updated with quantization_loss)

# 4. Promote to champion (manual or pipeline)
# Copy to: data/champions/test-app/default/rps_t3m/
├─ ppa_model.tflite
├─ scaler.pkl
├─ target_scaler.pkl
└─ ppa_model_metadata.json

# 5. Push to Kubernetes
$ ppa model push --app-name test-app --horizon rps_t3m

# ISSUE #1: If loader pod fails to build or start → push fails
# ISSUE #2: If scalers aren't regenerated inside pod → pickle incompatibility
```

#### Phase 2: Kubernetes Deployment

```yaml
# deploy/operator-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ppa-operator
spec:
  # ...
  volumes:
    - name: models
      persistentVolumeClaim:
        claimName: ppa-models   # ← Mounts PVC at /models

---
# deploy/predictiveautoscaler.yaml (CRD instance)
apiVersion: ppa.io/v1
kind: PredictiveAutoscaler
metadata:
  name: test-app-ppa
spec:
  targetDeployment: test-app
  appName: test-app
  horizon: rps_t3m
  maxReplicas: 10
  # modelPath, scalerPath optional - defaults to convention
```

**When Operator Pod Starts:**

1. kopf controller watches for PredictiveAutoscaler resources
2. Finds `test-app-ppa` CR
3. Calls `_parse_crd_spec()` → `_resolve_paths()`:
   - Looks for `/models/test-app/rps_t3m/ppa_model.tflite`
   - Looks for `/models/test-app/rps_t3m/scaler.pkl`
   - Looks for `/models/test-app/rps_t3m/target_scaler.pkl` (optional)
4. Creates `Predictor(model_path, scaler_path, target_scaler_path)`
5. **Predictor._try_load() tries to import TFLite runtime** ← **FAILS HERE**

#### Phase 3: Model Upgrade (PR#5 - History Preservation)

```python
# src/ppa/operator/main.py:155-174

if existing:
    # Model upgraded: preserve history, reload interpreter only
    old_history = existing.predictor.copy_history()
    
    new_predictor = Predictor(model_path, scaler_path, target_scaler_path)
    new_predictor.restore_history(old_history)
    
    existing.predictor = new_predictor  # Update in-place
```

**Workflow:**
1. User trains new model
2. Updates CR spec with new paths (or maintains convention)
3. Operator detects path mismatch: `paths_match()` returns False
4. Snapshots history (up to 60 steps ≈ 30 min at 30s intervals)
5. Creates new Predictor with new model
6. Restores history → avoids "coldstart" warmup period
7. **If new model TFLite load fails, history lost anyway**

---

## 5. Critical Gaps and Issues

### Gap #1: Incomplete TFLite Runtime Coverage

| Runtime | Source | Status | Notes |
|---------|--------|--------|-------|
| ai_edge_litert | `requirements.operator.txt` | ✅ Installed | Platform-specific binary issues |
| tensorflow.lite | ❌ Missing | ❌ Not installed | Would add 500MB to image |
| tflite-runtime | ❌ Missing | ❌ Not installed | Lightweight but not in requirements |

**Impact:** If `ai_edge_litert` wheel not available for platform → operator dies

### Gap #2: No Fallback or Graceful Degradation

When `Predictor._try_load()` fails:

```python
if not interpreter_loaded:
    raise RuntimeError(
        "No TFLite runtime found (tried ai_edge_litert, tensorflow.lite, tflite_runtime)"
    )
```

**No graceful fallback:**
- ❌ Not: "Run in observer mode until runtime installed"
- ❌ Not: "Use CPU-only TensorFlow"
- ❌ Not: "Wait and retry periodically"
- ✅ IS: Hard failure → operator becomes useless

### Gap #3: Model Path Convention Not Clearly Documented

Default path: `/models/{app_name}/{horizon}/ppa_model.tflite`

**Unclear aspects:**
1. What if `app_name` or `horizon` have special characters?
2. Is the path case-sensitive?
3. Can you override per-CR or is it global?
4. What's the exact PVC mount point?

### Gap #4: Scaler Pickle Compatibility Issue

**Issue:** Scalers trained on development machine with sklearn X, copied to PVC, loaded in operator pod with sklearn Y → incompatibility

**Solution (Already Implemented):** `regenerate_scalers.py`

```python
# src/ppa/runtime/regenerate_scalers.py

def _regenerate_single(app_name: str, horizon: str, df: pd.DataFrame) -> bool:
    """Regenerate scalers INSIDE pod for pickle compatibility."""
    base_dir = Path(f"/models/{app_name}/{horizon}")
    
    # Re-fit on training data
    scaler = MinMaxScaler()
    scaler.fit(df_clean[features].values)
    
    joblib.dump(scaler, base_dir / f"scaler_{horizon}.pkl", protocol=2)
```

**How it's invoked:** `ppa model push` command:

```python
# src/ppa/cli/commands/push.py:261-268

exec_result = exec_cmd(
    pod_path,
    "python3",
    "/tmp/regenerate.py",
    app_name,
    horizon_arg,
    "/tmp/training_data.csv",
)
```

**Problem:** If this fails → scalers not regenerated → pickle load errors in operator

### Gap #5: No Loader Pod Dockerfile Template

**File:** `src/ppa/cli/commands/push.py:170-194`

```python
loader_image = "ppa-loader:latest"
loader_dockerfile = PROJECT_DIR / "src" / "ppa" / "loader" / "Dockerfile"
if loader_dockerfile.exists():
    # Build custom loader image
    # ...
else:
    loader_image = "python:3.11-slim"  # Fallback
```

**Issue:** Dockerfile path `src/ppa/loader/Dockerfile` **doesn't exist**

- No custom loader image can be built
- Falls back to `python:3.11-slim`
- That image may not have required dependencies (pandas, sklearn, etc.)

### Gap #6: No Model Validation at Upload Time

When models are pushed to PVC, there's **no verification**:

```bash
$ ppa model push --app-name test-app  # ← Just copies files
                                      # ← Doesn't verify:
                                      # - File exists?
                                      # - Is valid TFLite?
                                      # - Metadata matches?
                                      # - Can load with ai_edge_litert?
```

**Better approach:**
```python
# Proposed: Validate model in loader pod before marking complete
exec_cmd(pod_path, "python3", "-c", """
import sys
sys.path.insert(0, '/app')
from ppa.operator.predictor import Predictor
try:
    p = Predictor(
        '/models/{app}/{horizon}/ppa_model.tflite',
        '/models/{app}/{horizon}/scaler.pkl'
    )
    if p.ready():
        print('✓ Model loads successfully')
    else:
        raise RuntimeError('Model not ready')
except Exception as e:
    print(f'✗ Model validation failed: {e}', file=sys.stderr)
    sys.exit(1)
""")
```

---

## 6. How to Fix the "No TFLite Runtime Found" Error

### Recommended Fixes (Priority Order)

#### Fix #1: Add Fallback TFLite Runtime to Operator Image (IMMEDIATE)

**Option A: Add tflite-runtime (lightweight, recommended)**

```diff
# requirements.operator.txt
  kopf==1.43.0
  kubernetes==35.0.0
  prometheus-client==0.23.1
  prometheus-api-client==0.7.0
  requests==2.32.5
  ai-edge-litert==2.1.3
+ tflite-runtime>=2.14.0  # Lightweight fallback
  numpy==1.26.0
  scikit-learn==1.3.2
  PyYAML==6.0.3
  python-dotenv==1.2.1
  rich==14.3.3
  typer==0.15.2
  python-json-logger==4.0.0
```

**Impact:** +40MB to operator image, but eliminates "No runtime found" error

#### Fix #2: Platform-Specific Binary Selection

Create separate operator images per architecture:

```dockerfile
# src/ppa/operator/Dockerfile.multi

FROM --platform=$BUILDPLATFORM python:3.11-slim AS builder

ARG BUILDPLATFORM
ARG TARGETPLATFORM

RUN case "$TARGETPLATFORM" in \
      "linux/amd64")   pip install ai-edge-litert==2.1.3 ;; \
      "linux/arm64")   pip install tflite-runtime==2.14.0 ;; \
      *)               pip install tflite-runtime==2.14.0 ;; \
    esac
```

#### Fix #3: Pre-flight Check Before Model Load

```python
# In predictor.py: _try_load()

@staticmethod
def check_runtime_available() -> str | None:
    """Check if ANY TFLite runtime is available."""
    for name, loader_fn in [
        ("ai_edge_litert", lambda: _load_ai_edge_litert_interpreter()),
        ("tensorflow.lite", lambda: __import__("tensorflow").lite.Interpreter),
        ("tflite_runtime", lambda: __import__("tflite_runtime.interpreter", fromlist=["Interpreter"]).Interpreter),
    ]:
        try:
            loader_fn()
            return name
        except:
            continue
    return None

def _try_load(self):
    if not (runtime := self.check_runtime_available()):
        logger.critical("No TFLite runtime available. Install one of:")
        logger.critical("  - ai-edge-litert")
        logger.critical("  - tensorflow>=2.14")
        logger.critical("  - tflite-runtime>=2.14")
        raise RuntimeError("No TFLite runtime found")
```

#### Fix #4: Model Validation in Push Command

```python
# src/ppa/cli/commands/push.py

def _validate_tflite_model(pod_path: str, model_path: str) -> bool:
    """Validate model can be loaded before marking push complete."""
    result = exec_cmd(
        pod_path, "python3", "-c", f"""
import sys
sys.path.insert(0, '/app')
try:
    from ppa.operator.predictor import Predictor
    p = Predictor('{model_path}', '{scaler_path}')
    print('✓ Model validated')
except Exception as e:
    print(f'✗ Error: {{e}}', file=sys.stderr)
    sys.exit(1)
    """
    )
    return result.returncode == 0
```

#### Fix #5: Create Missing Loader Dockerfile Template

```dockerfile
# src/ppa/loader/Dockerfile

FROM python:3.11-slim

WORKDIR /app

# For regenerate_scalers.py
RUN pip install --no-cache-dir \
    pandas>=2.0.0 \
    numpy>=1.26.0 \
    scikit-learn>=1.3.2 \
    joblib>=1.5.0

# Copy runtime scripts (regenerate_scalers.py)
COPY src/ppa/runtime/ ./ppa/runtime/
COPY src/ppa/common/ ./ppa/common/

CMD ["sleep", "300"]
```

---

## 7. Summary Table: Issues & Locations

| Issue | Root Cause | File(s) | Severity | Fix |
|-------|-----------|---------|----------|-----|
| No TFLite runtime | Missing from operator requirements | `requirements.operator.txt` | **CRITICAL** | Add tflite-runtime |
| Hard failure on missing runtime | No fallback logic | `predictor.py:166-169` | **HIGH** | Add graceful degradation |
| Scaler pickle incompatibility | Different sklearn versions | `push.py:261-268` | MEDIUM | Already has regenerate_scalers.py |
| No loader Dockerfile | Not created | Missing: `src/ppa/loader/Dockerfile` | MEDIUM | Create template |
| No model validation at upload | No verification | `push.py` | LOW | Add pre-upload validation |
| Unclear path conventions | Documentation gap | Config unclear | LOW | Document in CRD |

---

## 8. Files to Know

### Core Model Loading
- `src/ppa/operator/predictor.py` - Where "No TFLite runtime found" originates
- `src/ppa/operator/main.py` - Orchestrates CR → Predictor initialization
- `src/ppa/operator/Dockerfile` - Operator image, needs TFLite runtime

### Model Promotion
- `src/ppa/model/pipeline.py` - Train → Convert → Promote workflow
- `src/ppa/model/deployment.py` - Updates CR paths
- `src/ppa/cli/commands/push.py` - Copies models to PVC
- `src/ppa/runtime/regenerate_scalers.py` - Fixes pickle compatibility

### Configuration
- `src/ppa/config.py` - Path definitions, DEFAULT_MODEL_DIR = "/models"
- `src/ppa/model/artifacts.py` - Artifact path helpers
- `deploy/crd.yaml` - CRD definition with modelPath spec
- `deploy/operator-deployment.yaml` - PVC mounting

