# ML Pipeline Commands Reference

**Predictive Pod Autoscaler | ML Training & Model Deployment**

---

## Quick Start

```bash
# Install PPA
pip install -e .

# Train all three horizons with default settings
ppa model pipeline \
  --csv data/training-data/training_data_v2.csv \
  --horizons rps_t3m,rps_t5m,rps_t10m \
  --epochs 50

# Train with champion-challenger promotion
ppa model pipeline \
  --csv data/training-data/training_data_v2.csv \
  --horizons rps_t10m \
  --epochs 50 \
  --promote-if-better \
  --champion-dir data/champions \
  --promotion-metric smape \
  --promotion-gate 35 \
  --min-relative-improvement 2 \
  --max-underprov-regression 5
```

---

## Model Training

### Basic Training (Single Horizon)

```bash
ppa model train \
  --csv data/training-data/training_data_v2.csv \
  --target rps_t10m \
  --epochs 50 \
  --batch-size 32 \
  --patience 15 \
  --target-floor 5.0 \
  --test-split 0.1
```

**Output Artifacts:**
```
data/artifacts/
├── ppa_model_rps_t10m.keras
├── scaler_rps_t10m.pkl
├── target_scaler_rps_t10m.pkl
└── split_meta_rps_t10m.json
```

### Training Hyperparameters

| Flag | Default | Type | Description |
|---|---|---|---|
| `--csv` | — | str | Path to training CSV (required) |
| `--target` | `rps_t3m` | str | Target column (rps_t3m, rps_t5m, rps_t10m) |
| `--epochs` | 50 | int | Max training epochs |
| `--batch-size` | 32 | int | Samples per gradient update |
| `--patience` | 15 | int | Early stopping patience (epochs) |
| `--target-floor` | 5.0 | float | Min RPS floor for clipping |
| `--test-split` | 0.1 | float | Holdout test fraction (0-1) |
| `--output-dir` | `data/artifacts` | str | Output directory for model & scaler |

### Multi-Horizon Training

```bash
# Train all three horizons independently
for horizon in rps_t3m rps_t5m rps_t10m; do
  ppa model train \
    --csv data/training-data/training_data_v2.csv \
    --target "$horizon" \
    --epochs 50 \
    --patience 15 \
    --target-floor 5.0
done
```

**Models Trained:**
```
data/artifacts/
├── ppa_model_rps_t3m.keras     sMAPE: 23.14%
├── ppa_model_rps_t5m.keras     sMAPE: 20.39%
└── ppa_model_rps_t10m.keras    sMAPE: 18.57% ← Best
```

---

## Model Evaluation

### Evaluate Single Model

```bash
ppa model evaluate \
  --model data/artifacts/ppa_model_rps_t10m.keras \
  --scaler data/artifacts/scaler_rps_t10m.pkl \
  --target-scaler data/artifacts/target_scaler_rps_t10m.pkl \
  --csv data/training-data/training_data_v2.csv \
  --target rps_t10m \
  --metadata data/artifacts/split_meta_rps_t10m.json \
  --output-dir data/artifacts
```

**Output:**
```
Evaluation Results — rps_t10m
════════════════════════════════════════
Metric          │  Value
────────────────────────────────────────
sMAPE           │  16.72 %
Filtered MAPE   │  20.64 % (RPS > 10)
MAE             │  34.39 req/s
RMSE            │  51.29 req/s
────────────────────────────────────────
PPA (Predictive) vs HPA (Reactive)
────────────────────────────────────────
Avg Replicas    │  PPA: 6.03  HPA: 6.14
Over-provision  │  PPA: 75.8% HPA: 100%
Under-provision │  PPA: 24.2% HPA: 0%
Replica Savings │  1.68 %

eval_summary_rps_t10m.json written
```

### Evaluation Flags

| Flag | Type | Description |
|---|---|---|
| `--model` | str | Path to .keras model (required) |
| `--scaler` | str | Path to feature scaler .pkl (required) |
| `--target-scaler` | str | Path to target scaler .pkl (optional) |
| `--csv` | str | Training data CSV (required) |
| `--target` | str | Target column (required) |
| `--metadata` | str | Path to split_meta JSON (required for test split) |
| `--output-dir` | str | Output directory (default: data/artifacts) |
| `--low-traffic-threshold` | float | RPS threshold for filtered MAPE (default: 10) |

---

## Model Conversion (TFLite Quantization)

### Convert Single Model

```bash
ppa model convert \
  --model data/artifacts/ppa_model_rps_t10m.keras \
  --output data/artifacts/ppa_model_rps_t10m.tflite \
  --quantization float16
```

**Output:**
```
Converting ppa_model_rps_t10m.keras to TFLite...
  Input: FP32 keras model (~500 KB)
  Quantization: float16
  Output: ppa_model_rps_t10m.tflite (113 KB)
✅ Conversion successful
```

### Quantization Options

| Flag | Options | Description |
|---|---|---|
| `--quantization` | dynamic_range, int8, float16 | Quantization type (default: float16) |
| `--output` | str | Output .tflite path (required) |
| `--model` | str | Input .keras model (required) |

**Quantization Tradeoffs:**

| Method | Size | Speed | Accuracy | Inference Latency |
|--------|------|-------|----------|-------------------|
| **Dynamic Range** | ~50 KB | ⚡⚡⚡ | ⭐⭐ | <10ms |
| **Int8** | ~50 KB | ⚡⚡⚡ | ⭐⭐⭐ | <10ms |
| **Float16** | ~113 KB | ⚡⚡⚡ | ⭐⭐⭐⭐ | <10ms |
| **Unquantized** | ~500 KB | ⚡⚡ | ⭐⭐⭐⭐⭐ | ~20ms |

**Recommendation:** Use **float16** (default) — balances size and accuracy for RPS predictions.

---

## End-to-End Pipeline Orchestration

### Full Training → Evaluation → Conversion

```bash
ppa model pipeline \
  --csv data/training-data/training_data_v2.csv \
  --horizons rps_t3m,rps_t5m,rps_t10m \
  --epochs 50 \
  --output-dir data/artifacts \
  --quality-gate 35
```

**Output:**
```
Pipeline: Training multi-horizon LSTM models
════════════════════════════════════════════════════════════════
Horizon    │ Rows   │ sMAPE   │ MAE    │ RMSE   │ Size   │ Status
────────────────────────────────────────────────────────────────
rps_t3m    │ 1,602  │ 23.14%  │ 50.81  │ 76.13  │ 113 KB │ ✅ PASS
rps_t5m    │ 1,602  │ 20.39%  │ 49.97  │ 74.07  │ 113 KB │ ✅ PASS
rps_t10m   │ 1,602  │ 18.57%  │ 35.97  │ 52.91  │ 113 KB │ ✅ PASS
────────────────────────────────────────────────────────────────
All models trained and converted. Artifacts in data/artifacts/
```

### Pipeline with Champion-Challenger Promotion

```bash
# Run pipeline AND promote winning horizon to champion dir
ppa model pipeline \
  --csv data/training-data/training_data_v2.csv \
  --horizons rps_t10m \
  --epochs 50 \
  --promote-if-better \
  --champion-dir data/champions \
  --promotion-metric smape \
  --promotion-gate 35.0 \
  --min-relative-improvement 2.0 \
  --max-underprov-regression 5.0 \
  --promote-cr-name test-app-ppa \
  --promote-cr-namespace default
```

**Output:**
```
Pipeline: Training multi-horizon LSTM models (with promotion)
════════════════════════════════════════════════════════════════

[Training]
rps_t10m: Keras model → 50 epochs → sMAPE 18.57% ✅

[Evaluation]
sMAPE: 18.57% (gate: 35.0%) → PASS ✅

[Conversion]
ppa_model_rps_t10m.keras → ppa_model_rps_t10m.tflite (113 KB) ✅

[Champion-Challenger Policy]
No previous champion → Promoting rps_t10m as new champion

[Promotion]
✅ Copied ppa_model.tflite to data/champions/rps_t10m/
✅ Copied scaler.pkl to data/champions/rps_t10m/
✅ Copied target_scaler.pkl to data/champions/rps_t10m/
✅ Copied eval_summary.json to data/champions/rps_t10m/
✅ Patched PredictiveAutoscaler CR test-app-ppa:
     modelPath: /models/test-app/ppa_model.tflite
     scalerPath: /models/test-app/scaler.pkl
     targetScalerPath: /models/test-app/target_scaler.pkl

Operator will reload on next 30s cycle
```

### Pipeline Flags

| Flag | Default | Type | Description |
|---|---|---|---|
| `--csv` | — | str | Training data CSV (required) |
| `--horizons` | rps_t3m,rps_t5m,rps_t10m | str | Comma-separated horizons to train |
| `--epochs` | 50 | int | Training epochs |
| `--output-dir` | data/artifacts | str | Output directory |
| `--quality-gate` | 35.0 | float | sMAPE threshold (%) — fail if exceeded |
| `--gate-metric` | smape | str | Which metric for gate (smape, mape_filtered, mae) |
| `--patience` | 15 | int | Early stopping patience |
| `--target-floor` | 5.0 | float | Min RPS floor |
| `--promote-if-better` | false | flag | Enable champion-challenger promotion |
| `--champion-dir` | data/champions | str | Champion storage directory |
| `--promotion-metric` | smape | str | Optimization metric (smape, mape_filtered, mae) |
| `--promotion-gate` | 35.0 | float | Quality gate for promotion (%) |
| `--min-relative-improvement` | 2.0 | float | Min % improvement to promote (%) |
| `--max-underprov-regression` | 5.0 | float | Max under-provisioning regression (%) |
| `--promote-cr-name` | test-app-ppa | str | CR name to patch on promotion |
| `--promote-cr-namespace` | default | str | CR namespace |

---

## Data Validation

### Validate Training Data Quality

```bash
ppa data validate data/training-data/training_data_v2.csv
```

**Output:**
```
Validating training_data_v2.csv
════════════════════════════════════════════════════
✅ File exists: 12,800 rows × 20 columns
✅ All required features present
✅ No NaN values in numeric columns
✅ Reasonable value ranges:
    RPS: 0 → 550 req/s ✅
    CPU: 0 → 100 % ✅
    Memory: 0 → 100 % ✅
    Latency: 0 → 500 ms ✅
✅ Sufficient data for LSTM (>3,000 rows needed, have 12,800)
✅ Time gaps handled (21 segments detected)
════════════════════════════════════════════════════
Status: READY FOR TRAINING ✅
```

### Verify Feature Extraction

```bash
python -m ppa.dataflow.verify_features
```

**Output:**
```
Checking Prometheus feature readiness...
✅ Prometheus healthy
✅ All 14 features extractable
✅ PromQL queries returning data
✅ Ready to run data collection
```

---

## Testing

### Run ML Tests

```bash
# All tests
python -m pytest tests/test_train.py tests/test_evaluate.py tests/test_convert.py -v

# Specific test
pytest tests/test_train.py::TestCreateDatasetFromSegments::test_correct_window_shape -v

# Test coverage
pytest tests/ --cov=ppa.model --cov-report=html
```

---

## Troubleshooting

### Model Training Issues

**Problem:** Early stopping triggers too early
```
Solution: Increase --patience (try 15, 20, 30)
ppa model train --csv ... --patience 30
```

**Problem:** NaN loss during training
```
Solution: Check for data quality or try reducing learning rate
ppa model train --csv ... --target-floor 5.0  # Clamp targets
```

**Problem:** Model predictions are all zeros or negative
```
Solution: Target scaler may not be fitted correctly
Check: ls -la data/artifacts/target_scaler*.pkl
```

### Evaluation Issues

**Problem:** sMAPE reported as very high (>100%)
```
Solution: This is correct for near-zero RPS rows
Use --low-traffic-threshold 10 to filter them
```

**Problem:** Evaluation crashes with "Split metadata not found"
```
Solution: Pass --metadata flag with split_meta JSON
ppa model evaluate ... --metadata data/artifacts/split_meta_rps_t10m.json
```

### Deployment Issues

**Problem:** Operator doesn't detect promotion
```
Solution: Ensure CR name matches:
kubectl get ppa --all-namespaces
Then use correct --promote-cr-name and --promote-cr-namespace
```

**Problem:** TFLite model crashes on inference
```
Solution: Check quantization type and model shape
ppa model convert --quantization float16  # Use default
```

---

## See Also

- [ML Pipeline Architecture](../architecture/ml_pipeline.md) — Design decisions and flow
- [Operator Commands Reference](./operator_commands.md) — Deployment and debugging
- [Data Collection Reference](../architecture/data_collection.md) — Training data generation
