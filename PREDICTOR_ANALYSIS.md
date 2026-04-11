# Predictor Class Analysis

## File Locations

### Predictor Class Definition
- **File**: `/run/media/vatsal/Drive/Projects/predictive_pod_autoscaler/src/ppa/operator/predictor.py`
- **Class Definition**: Line 37
- **Total Lines**: 504

### Related Functions
- **_get_or_create_state()**: `/run/media/vatsal/Drive/Projects/predictive_pod_autoscaler/src/ppa/operator/main.py` (Line 131)
- **_parse_crd_spec()**: `/run/media/vatsal/Drive/Projects/predictive_pod_autoscaler/src/ppa/operator/main.py` (Line 204)

---

## Predictor Class Methods - Complete List

| Method | Line | Signature | Purpose |
|--------|------|-----------|---------|
| `__init__` | 40 | `(model_path, scaler_path, target_scaler_path=None)` | Initialize predictor with paths to model, scaler, target scaler |
| `_load_and_validate_metadata()` | 67 | `()` | Load and validate model metadata from JSON file |
| `_try_load()` | 126 | `()` | Attempt to load model, scaler, target scaler with exponential backoff |
| `paths_match()` | 254 | `(model_path, scaler_path, target_scaler_path=None) -> bool` | **[Path Matching]** Check if model paths match current paths (model upgrade detection) |
| `copy_history()` | 264 | `() -> list` | Return a copy of current history for preservation during model upgrade |
| `restore_history()` | 268 | `(history_snapshot: list) -> None` | Restore history from a snapshot during model upgrade |
| `update()` | 275 | `(features: dict) -> None` | Append features row to rolling history deque |
| `ready()` | 279 | `() -> bool` | Check if predictor is ready (history filled + model loaded + scaler loaded) |
| `predict()` | 289 | `() -> float` | Generate RPS prediction using LSTM model |
| `serialize_history()` | 320 | `() -> list[list[float]] \| None` | Serialize history to JSON-compatible format for CR status storage |
| `deserialize_history()` | 330 | `(serialized: list[list[float]]) -> bool` | Restore history from serialized format (pod restart resilience) |
| `get_history_summary()` | 345 | `() -> dict` | Get summary of history state for CR status monitoring |
| `prefill_history()` | 362 | `(feature_rows: list) -> None` | Populate history deque from list of feature dictionaries (e.g., Prometheus range query) |
| `track_prediction_accuracy()` | 372 | `(predicted_rps: float, actual_rps: float) -> None` | Track prediction vs actual RPS for concept drift detection |
| `check_concept_drift()` | 379 | `() -> dict` | Detect concept drift by comparing predicted vs actual RPS (returns dict with detected/error_pct/severity) |
| `should_trigger_retraining()` | 445 | `(drift_severity: str, error_pct: float) -> dict` | Determine if retraining should be triggered based on drift severity (returns dict with trigger/reason/action) |
| `reset_retraining_flag()` | 487 | `() -> None` | Reset retraining flag after retraining job completes |

---

## Predictor Class Attributes

### Public Attributes
- `model_path` (str): Path to TFLite model file
- `scaler_path` (str): Path to feature scaler (joblib)
- `target_scaler_path` (str \| None): Path to target scaler (joblib)
- `history` (deque): Rolling window of feature vectors (maxlen=LOOKBACK_STEPS)
- `interpreter` (tflite.Interpreter \| None): TFLite model interpreter
- `scaler` (StandardScaler \| None): Feature scaler for normalization
- `target_scaler` (StandardScaler \| None): Target output scaler for denormalization
- `input_details` (list[dict] \| None): TFLite input tensor metadata
- `output_details` (list[dict] \| None): TFLite output tensor metadata
- `lookback` (int): Number of historical steps to feed to LSTM (initialized from LOOKBACK_STEPS)

### Internal State Attributes
- `_load_failed` (bool): Flag indicating model load failure
- `_load_failures` (int): Count of consecutive load failures (for exponential backoff)
- `_last_load_attempt` (float): Timestamp of last load attempt
- `prediction_history` (deque): Last 60 predictions (30 min at 30s interval)
- `actual_history` (deque): Last 60 actual RPS values
- `concept_drift_detected` (bool): Current concept drift state
- `last_drift_check_time` (float): Timestamp of last drift check
- `_severe_drift_start_time` (float \| None): When severe drift started
- `_retraining_triggered` (bool): Flag if retraining already triggered

---

## Recent Changes - Latest Commit (45d9c6b)

**Commit**: "Add paths_match method to Predictor for model upgrade detection"
**Date**: Sat Apr 11 11:32:21 2026
**Changes**:
1. **Added `paths_match()` method** (Line 254-262) for model upgrade detection
   - Used by `_get_or_create_state()` to detect when model paths change
   - Returns bool comparing all three paths: model_path, scaler_path, target_scaler_path

2. **Enhanced error tracking** in `_try_load()`:
   - Added `load_step` variable to track which stage of loading fails
   - Granular logging for each step: metadata_validation, interpreter_load, scaler_load, target_scaler_load, tensor_details
   - Better error messages showing exactly where failures occur

3. **Improved logging**:
   - Changed log messages from bare strings to structured "✓ {load_step} succeeded" format
   - Added more detailed debug logging during tensor details retrieval

---

## _get_or_create_state() Function (main.py, Line 131)

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

### How it Uses Predictor
1. **Line 151**: Calls `existing.predictor.paths_match(model_path, scaler_path, target_scaler_path)` to check if model paths have changed
   - If paths match → returns existing state unchanged
   - If paths differ → triggers model upgrade logic

2. **Model Upgrade Flow (Lines 155-174)**:
   - Line 160: `old_history = existing.predictor.copy_history()` — snapshots current history
   - Line 164: `new_predictor = Predictor(model_path, scaler_path, target_scaler_path)` — creates new predictor with upgraded paths
   - Line 167: `new_predictor.restore_history(old_history)` — restores history to new predictor to avoid 30-min warmup blindness
   - Line 170: `existing.predictor = new_predictor` — updates state in-place

3. **First-Time Initialization (Lines 177-201)**:
   - Line 178: `predictor=Predictor(model_path, scaler_path, target_scaler_path)` — creates new predictor
   - Lines 183-194: If persisted_history exists (pod restart), calls `state.predictor.deserialize_history(history_data)`
   - Line 196-198: Otherwise logs that prefill is skipped to avoid scaler distribution mismatch

---

## _parse_crd_spec() Function (main.py, Line 204)

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

### Context
- Parses CRD spec and calls `_get_or_create_state()` (Line 258)
- Extracts paths from spec using `_resolve_paths()` (Line 256)
- Calls `_get_or_create_state()` with:
  - `key = (cr_ns, cr_name)`
  - `model_path, scaler_path, target_scaler_path` from `_resolve_paths()`
  - `config` dict with scaling parameters
  - `persisted_history` from `status.get("historySnapshot")` for pod restart resilience

---

## Method Call Analysis - What's Called vs What Exists

### Methods DEFINED in Predictor Class
✅ `__init__` — Line 40
✅ `_load_and_validate_metadata` — Line 67
✅ `_try_load` — Line 126
✅ `paths_match` — Line 254 ← **ADDED in latest commit**
✅ `copy_history` — Line 264
✅ `restore_history` — Line 268
✅ `update` — Line 275
✅ `ready` — Line 279
✅ `predict` — Line 289
✅ `serialize_history` — Line 320
✅ `deserialize_history` — Line 330
✅ `get_history_summary` — Line 345
✅ `prefill_history` — Line 362
✅ `track_prediction_accuracy` — Line 372
✅ `check_concept_drift` — Line 379
✅ `should_trigger_retraining` — Line 445
✅ `reset_retraining_flag` — Line 487

### Methods CALLED from main.py
1. **Line 151**: `existing.predictor.paths_match()` ✅ DEFINED (Line 254)
2. **Line 160**: `existing.predictor.copy_history()` ✅ DEFINED (Line 264)
3. **Line 167**: `new_predictor.restore_history()` ✅ DEFINED (Line 268)
4. **Line 186**: `state.predictor.deserialize_history()` ✅ DEFINED (Line 330)
5. **Line 358**: `state.predictor.update()` ✅ DEFINED (Line 275)
6. **Line 359**: `len(state.predictor.history)` ✅ DEFINED (Line 44, attribute)
7. **Line 360**: `state.predictor.history.maxlen` ✅ DEFINED (deque attribute)
8. **Line 366**: `state.predictor._load_failed` ✅ DEFINED (Line 50, attribute)
9. **Line 372**: `state.predictor.serialize_history()` ✅ DEFINED (Line 320)
10. **Line 380**: `state.predictor.ready()` ✅ DEFINED (Line 279)
11. **Line 381**: `state.predictor._load_failed` ✅ DEFINED (Line 50, attribute)
12. **Line 413**: `state.predictor.predict()` ✅ DEFINED (Line 289)
13. **Line 421**: `state.predictor.track_prediction_accuracy()` ✅ DEFINED (Line 372)
14. **Line 423**: `state.predictor.check_concept_drift()` ✅ DEFINED (Line 379)
15. **Line 438**: `state.predictor.should_trigger_retraining()` ✅ DEFINED (Line 445)

---

## Summary

### Predictor Class Status: ✅ COMPLETE
- **Total Methods**: 17 (including 2 private helper methods)
- **Public Methods**: 14 (excluding __init__)
- **Private Methods**: 2 (_load_and_validate_metadata, _try_load)
- **Latest Addition**: `paths_match()` method for model upgrade detection (Commit 45d9c6b)

### Path Matching Implementation
The `paths_match()` method (Line 254-262) provides:
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

This is the **KEY METHOD** for path matching mentioned in the search request. It enables detection of model upgrades by comparing all three paths: model_path, scaler_path, and target_scaler_path.

### All Called Methods Exist
✅ **All 15 methods called from main.py are properly defined in the Predictor class**
✅ **All attributes accessed are properly initialized**
✅ **No missing or unimplemented methods**

