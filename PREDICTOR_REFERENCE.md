# Predictor Class - Complete Reference Guide

## File Structure

```
src/ppa/operator/
├── predictor.py         <- Predictor class definition
├── main.py              <- Usage of Predictor via _get_or_create_state()
├── scaler.py
├── features.py
├── diagnostics.py
└── config.py
```

## 1. Predictor Class Definition

### Location
- **File**: `src/ppa/operator/predictor.py`
- **Line**: 37 (class definition)
- **Lines**: 1-504 (entire file)

### Constructor
```python
def __init__(
    self, 
    model_path: str, 
    scaler_path: str, 
    target_scaler_path: str | None = None
):
```

**Initializes**:
- TFLite model interpreter
- Feature scaler (joblib)
- Optional target scaler (joblib)
- Rolling history deque
- Concept drift tracking
- Exponential backoff counters

---

## 2. Path Matching Method (PRIMARY FOCUS)

### Method: `paths_match()`

**Location**: Line 254-262

**Signature**:
```python
def paths_match(
    self, 
    model_path: str, 
    scaler_path: str, 
    target_scaler_path: str | None = None
) -> bool:
```

**Purpose**: 
Detect model upgrades by comparing if provided paths match current instance paths.

**Returns**: 
`bool` - True if all three paths match exactly, False otherwise

**Implementation**:
```python
def paths_match(
    self, model_path: str, scaler_path: str, target_scaler_path: str | None = None
) -> bool:
    """Check if model paths match current paths (used to detect model upgrades)."""
    return (
        self.model_path == model_path
        and self.scaler_path == scaler_path
        and self.target_scaler_path == target_scaler_path
    )
```

**Used By**:
- `_get_or_create_state()` at `/src/ppa/operator/main.py:151`

**Usage Pattern**:
```python
# In _get_or_create_state()
existing = _cr_state.get(key)
if existing and existing.predictor.paths_match(model_path, scaler_path, target_scaler_path):
    # Model hasn't changed - reuse existing state
    return existing
else:
    # Model changed - trigger upgrade with history preservation
    ...
```

**Recent Change**: 
Added in commit `45d9c6b` (Sat Apr 11 11:32:21 2026)

---

## 3. Complete Method Reference

### Model Loading & Validation

#### `_load_and_validate_metadata()` - Line 67
**Purpose**: Load and validate model metadata from JSON file
**Returns**: dict | None
**Handles**:
- Feature column mismatch (CRITICAL - re-raised)
- Lookback mismatch (WARNING - logged)
- Quantization loss > 5% (WARNING - logged)
- Missing metadata (WARNING - backward compat)

#### `_try_load()` - Line 126
**Purpose**: Attempt to load model, scaler, and target scaler with exponential backoff
**Returns**: None
**Key Features**:
- Idempotent (returns if already loaded)
- Exponential backoff on failures (capped at 5 min)
- Tries three TFLite loaders in order: ai_edge_litert, tensorflow.lite, tflite_runtime
- Granular error tracking per load stage

### History Management

#### `copy_history()` - Line 264
**Purpose**: Return copy of current history for preservation during model upgrade
**Returns**: list
**Used**: During model upgrade to snapshot history before creating new Predictor

#### `restore_history(history_snapshot: list)` - Line 268
**Purpose**: Restore history from snapshot after model upgrade
**Returns**: None
**Used**: Restores history to new Predictor to avoid 30-min warmup period

#### `serialize_history()` - Line 320
**Purpose**: Serialize history to JSON-compatible format for CR status storage
**Returns**: list[list[float]] | None
**Used**: Store history in CR status for pod restart resilience

#### `deserialize_history(serialized: list[list[float]])` - Line 330
**Purpose**: Restore history from serialized format
**Returns**: bool (True if successful)
**Used**: Restore history from CR status on pod restart

#### `prefill_history(feature_rows: list)` - Line 362
**Purpose**: Populate history deque from list of feature dictionaries
**Returns**: None
**Note**: Currently SKIPPED in _get_or_create_state() to avoid scaler distribution mismatch

### Prediction & State

#### `update(features: dict)` - Line 275
**Purpose**: Append features row to rolling history deque
**Returns**: None
**Called**: Every reconciliation cycle in main.py:358

#### `ready()` - Line 279
**Purpose**: Check if predictor is ready for prediction
**Returns**: bool
**Checks**:
- History filled to lookback length
- Interpreter loaded
- Scaler loaded
- Retries loading if previous attempt failed

#### `predict()` - Line 289
**Purpose**: Generate RPS prediction using LSTM model
**Returns**: float (predicted RPS, clamped to 0.0 minimum)
**Steps**:
1. Check readiness
2. Get last `lookback` history rows
3. Normalize with scaler
4. Invoke TFLite interpreter
5. Inverse-transform output with target scaler (or fallback)
6. Track inference latency

### Concept Drift & Retraining

#### `track_prediction_accuracy(predicted_rps, actual_rps)` - Line 372
**Purpose**: Track prediction vs actual RPS for drift detection
**Returns**: None
**Stores**: In prediction_history and actual_history deques (maxlen=60)

#### `check_concept_drift()` - Line 379
**Purpose**: Detect concept drift by comparing predicted vs actual RPS
**Returns**: dict with keys:
- `detected` (bool)
- `error_pct` (float) - Mean Absolute Percentage Error
- `severity` (str) - "normal", "moderate", or "severe"
- `checked` (bool)
**Thresholds**:
- 20%+ error = drift detected
- 50%+ error = severe drift
- Checks only every 5 minutes to avoid log spam

#### `should_trigger_retraining(drift_severity, error_pct)` - Line 445
**Purpose**: Determine if retraining should be triggered
**Returns**: dict with keys:
- `trigger` (bool)
- `reason` (str)
- `suggested_action` (str)
- `drift_duration_minutes` (float)
**Logic**: Triggers retraining if severe drift persists > 1 hour

#### `reset_retraining_flag()` - Line 487
**Purpose**: Reset retraining flag after retraining job completes
**Returns**: None
**Called**: After successful retraining

### Utility Methods

#### `get_history_summary()` - Line 345
**Purpose**: Get summary of history state for CR status monitoring
**Returns**: dict with:
- `filled_steps` (int)
- `max_steps` (int)
- `ready` (bool)
- `last_drift_check` (str, ISO format)
- `drift_detected` (bool)

---

## 4. _get_or_create_state() Integration

### Location
- **File**: `src/ppa/operator/main.py`
- **Line**: 131-201
- **Called from**: `_parse_crd_spec()` at line 258

### Signature
```python
def _get_or_create_state(
    key: tuple[str, str],
    model_path: str,
    scaler_path: str,
    target_scaler_path: str | None = None,
    config: dict[str, Any] | None = None,
    target_app: str = "",
    target_ns: str = "",
    max_r: int = 1,
    container_name: str | None = None,
    min_r: int = 1,
    persisted_history: dict | None = None,
) -> CRState:
```

### Predictor Usage Flow

```
┌─────────────────────────────────────────────┐
│ _get_or_create_state() called               │
│ with key, model_path, scaler_path           │
└──────────────┬──────────────────────────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ Check if state exists?   │
    └──────┬─────────────┬──────┘
           │             │
        NO │             │ YES
           │             │
           ▼             ▼
    ┌──────────────────────────────────────┐
    │ paths_match() returns True?          │
    │ (Line 151)                           │
    └──┬──────────────────────────────┬────┘
       │                              │
    YES│ Match                    NO │ Changed
       │                              │
       ▼                              ▼
    ┌──────────────┐        ┌────────────────────┐
    │ Return       │        │ Model Upgrade Flow │
    │ existing     │        │ (Lines 155-174)    │
    │ state        │        └────┬───────────────┘
    │ unchanged    │             │
    └──────────────┘             ▼
                        ┌────────────────────────┐
                        │ old_history =          │
                        │ copy_history()         │
                        │ (Line 160)             │
                        └────┬───────────────────┘
                             │
                             ▼
                        ┌────────────────────────┐
                        │ new_predictor =        │
                        │ Predictor(new_paths)   │
                        │ (Line 164)             │
                        └────┬───────────────────┘
                             │
                             ▼
                        ┌────────────────────────┐
                        │ restore_history()      │
                        │ (Line 167)             │
                        └────┬───────────────────┘
                             │
                             ▼
                        ┌────────────────────────┐
                        │ Update predictor in    │
                        │ existing.predictor     │
                        │ (Line 170)             │
                        └────────────────────────┘
    
    ┌──────────────────────────────────────────────┐
    │ First-Time Initialization (Lines 177-201)    │
    ├──────────────────────────────────────────────┤
    │ Create CRState with new Predictor            │
    │                                              │
    │ IF persisted_history exists:                 │
    │   deserialize_history()  (Line 186)          │
    │ ELSE:                                        │
    │   Skip prefill (scaler distribution mismatch)│
    └──────────────────────────────────────────────┘
```

### Key Predictor Methods Called

1. **Line 151**: `existing.predictor.paths_match(model_path, scaler_path, target_scaler_path)`
   - Detects if model paths have changed
   - If True → return existing state (no changes)
   - If False → trigger model upgrade

2. **Line 160**: `existing.predictor.copy_history()`
   - Snapshot current history
   - Preserves prediction context across model upgrade

3. **Line 164**: `Predictor(model_path, scaler_path, target_scaler_path)`
   - Create new Predictor instance with upgraded paths
   - Calls `_try_load()` internally

4. **Line 167**: `new_predictor.restore_history(old_history)`
   - Restore snapshot history to new Predictor
   - Prevents 30-minute warmup period after upgrade

5. **Line 186**: `state.predictor.deserialize_history(history_data)`
   - Restore history from CR status
   - Enables pod restart resilience

---

## 5. _parse_crd_spec() Integration

### Location
- **File**: `src/ppa/operator/main.py`
- **Line**: 204-272
- **Called from**: `reconcile()` at line 543

### Signature
```python
def _parse_crd_spec(
    spec: dict[str, Any],
    status: dict[str, Any] | None,
    meta: dict[str, Any],
    cr_ns: str,
    cr_name: str,
) -> tuple[dict[str, Any], CRState]:
```

### Predictor Integration

```python
# Line 256: Resolve paths from CRD spec
model_path, scaler_path, target_scaler_path = _resolve_paths(
    spec, target_app, target_horizon
)

# Line 257: Extract persisted history from CR status
persisted_history = status.get("historySnapshot") if status else None

# Line 258: Get or create state (with path matching)
state = _get_or_create_state(
    key=(cr_ns, cr_name),
    model_path=model_path,
    scaler_path=scaler_path,
    target_scaler_path=target_scaler_path,
    config=config,
    target=target,
    target_ns=target_ns,
    max_r=max_r,
    container_name=container_name,
    min_r=min_r,
    persisted_history=persisted_history,
)
```

---

## 6. Recent Changes Summary

### Commit: 45d9c6b
**Message**: "Add paths_match method to Predictor for model upgrade detection"
**Date**: Sat Apr 11 11:32:21 2026
**Author**: Vatsal Umrania

### Changes:
1. **Added paths_match() method** (Line 254-262)
   - Compares model_path, scaler_path, target_scaler_path
   - Returns bool indicating if paths match
   - Enables model upgrade detection in _get_or_create_state()

2. **Enhanced _try_load() error tracking**
   - Added `load_step` variable to track which stage fails
   - Granular logging for each stage
   - Better error messages showing exact failure point

3. **Improved logging format**
   - Changed from bare strings to structured "✓ {load_step} succeeded"
   - More detailed debug logging during tensor retrieval

---

## 7. Method Verification

### All Called Methods - Verification Matrix

| Method | File | Line | Defined | Status |
|--------|------|------|---------|--------|
| paths_match() | main.py | 151 | predictor.py | 254 | ✅ |
| copy_history() | main.py | 160 | predictor.py | 264 | ✅ |
| restore_history() | main.py | 167 | predictor.py | 268 | ✅ |
| deserialize_history() | main.py | 186 | predictor.py | 330 | ✅ |
| update() | main.py | 358 | predictor.py | 275 | ✅ |
| ready() | main.py | 380 | predictor.py | 279 | ✅ |
| predict() | main.py | 413 | predictor.py | 289 | ✅ |
| serialize_history() | main.py | 372 | predictor.py | 320 | ✅ |
| track_prediction_accuracy() | main.py | 421 | predictor.py | 372 | ✅ |
| check_concept_drift() | main.py | 423 | predictor.py | 379 | ✅ |
| should_trigger_retraining() | main.py | 438 | predictor.py | 445 | ✅ |

**Result**: ✅ ALL 11 CALLED METHODS ARE PROPERLY DEFINED

---

## 8. Attributes Reference

### Core Configuration
- `model_path: str` - Path to TFLite model file
- `scaler_path: str` - Path to feature scaler joblib
- `target_scaler_path: str | None` - Path to target scaler joblib

### Loaded Components
- `interpreter: tflite.Interpreter | None` - TFLite model interpreter
- `scaler: StandardScaler | None` - Feature normalization scaler
- `target_scaler: StandardScaler | None` - Output denormalization scaler
- `input_details: list[dict] | None` - TFLite input tensor metadata
- `output_details: list[dict] | None` - TFLite output tensor metadata
- `lookback: int` - Number of historical steps for LSTM

### History State
- `history: deque` - Rolling window of features (maxlen=LOOKBACK_STEPS)
- `prediction_history: deque` - Last 60 predictions (maxlen=60)
- `actual_history: deque` - Last 60 actual RPS values (maxlen=60)

### Load State
- `_load_failed: bool` - Flag indicating load failure
- `_load_failures: int` - Count of consecutive failures
- `_last_load_attempt: float` - Timestamp of last load attempt

### Drift State
- `concept_drift_detected: bool` - Current drift status
- `last_drift_check_time: float` - Timestamp of last check
- `_severe_drift_start_time: float | None` - When severe drift started
- `_retraining_triggered: bool` - If retraining already triggered

---

## 9. Integration Points

### Called From
- `_get_or_create_state()` - main.py:131
- `_update_predictor_state()` - main.py:344
- `_make_scaling_decision()` - main.py:399

### Calls Out
- `joblib.load()` - Load scalers
- `ai_edge_litert`, `tensorflow.lite`, `tflite_runtime` - Load model
- `numpy` - Vector operations
- `logging` - Diagnostic output

### Uses From
- `ppa.config` - LOOKBACK_STEPS, config constants
- `ppa.common.feature_spec` - FEATURE_COLUMNS, NUM_FEATURES
- `ppa.operator.diagnostics` - validate_model_files(), diagnose_model_load_issue()

---

## 10. Summary

### Predictor Class Status
- **Total Methods**: 17 (15 public + 2 private)
- **Latest Addition**: `paths_match()` for model upgrade detection
- **Key Feature**: History preservation across model upgrades
- **Verification**: All called methods properly defined

### Path Matching Implementation
- **Method**: `paths_match(model_path, scaler_path, target_scaler_path)`
- **Line**: 254-262
- **Purpose**: Detect model path changes for upgrade handling
- **Used by**: `_get_or_create_state()` at line 151 of main.py

### Integration Quality
- ✅ No missing methods
- ✅ No unimplemented interfaces
- ✅ All attributes initialized properly
- ✅ Comprehensive error handling
- ✅ Proper state management for model upgrades

