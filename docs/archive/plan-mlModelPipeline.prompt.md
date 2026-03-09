## Plan: ML Model Pipeline — Multi-Horizon LSTM

**TL;DR:** Build a complete ML pipeline that trains, evaluates, and converts LSTM models for all 3 RPS forecast horizons (3m, 5m, 10m). This involves: making `model/train.py` horizon-aware, fully implementing `model/evaluate.py` with metrics + plots + HPA comparison, creating a new orchestration script `model/pipeline.py` to chain all stages, and adding unit tests. The 12,452-row dataset is sufficient to start training while collection continues.

**Steps**

1. **Refactor `model/train.py` for multi-horizon support**
   - Add `--target` CLI arg accepting values like `rps_t3m`, `rps_t5m`, `rps_t10m` (default: `rps_t3m`)
   - Replace the hardcoded `TARGET_COL = TARGET_COLUMNS[0]` with the CLI-driven value
   - Change artifact output names to include horizon: `ppa_model_rps_t3m.keras`, `scaler_rps_t3m.pkl`
   - Make `train_model()` **return** `(model, scaler, history, metrics_dict)` instead of only printing — downstream stages need these
   - Add a `--test-split` arg (default `0.1`) to hold out a final test set beyond the existing 80/20 train/val split → producing a 70/20/10 split (train/val/test), saving the test indices for `evaluate.py`
   - Save split metadata (test start index, target column, lookback) as `model/artifacts/split_meta_{horizon}.json` so evaluation can reproduce the exact test set

2. **Implement `model/evaluate.py` — metrics, plots, HPA comparison**
   - **Inputs:** `--model` (`.keras` path), `--scaler` (`.pkl` path), `--csv`, `--target`, `--output-dir` (default `model/artifacts/`)
   - **Metric computation:** Load the held-out test set using saved split metadata, run predictions, compute:
     - MAPE (Mean Absolute Percentage Error)
     - MAE (Mean Absolute Error)
     - RMSE (Root Mean Squared Error)
   - **Predicted vs Actual plot:** Time-series line chart (matplotlib) with actual RPS and predicted RPS overlaid. Save as `eval_pred_vs_actual_{horizon}.png`
   - **PPA vs HPA comparison:**
     - *PPA replicas:* `ceil(predicted_rps / CAPACITY_PER_POD)`, clamped to `[min_replicas, max_replicas]`
     - *HPA replicas:* reactive baseline using actual RPS at time `t` (no lookahead), same clamping
     - Compute: avg replicas (PPA vs HPA), over-provisioning rate, under-provisioning rate, wasted pod-seconds
     - Generate a dual-axis plot: replica counts over time + RPS. Save as `eval_ppa_vs_hpa_{horizon}.png`
   - **Summary table:** Print a formatted table with all metrics + comparison stats. Also save as `eval_summary_{horizon}.json`
   - Return a structured `EvalResult` dict for the orchestrator

3. **Minor update to `model/convert.py`**
   - Accept `--output` arg to specify the `.tflite` output path (currently auto-derived)
   - This enables the orchestrator to place horizon-specific artifacts: `ppa_model_rps_t3m.tflite`
   - No logic changes needed — conversion is already correct

4. **Create `model/pipeline.py` — orchestration script**
   - CLI: `--csv` (path to training CSV), `--horizons` (default `rps_t3m,rps_t5m,rps_t10m`), `--epochs`, `--output-dir`, `--quality-gate` (MAPE threshold, default `25.0`)
   - For each selected horizon:
     1. **Train** → call `train_model()` with the target column
     2. **Evaluate** → call `evaluate_model()` on the test split
     3. **Quality gate** → if MAPE exceeds threshold, log a warning but continue (don't block other horizons)
     4. **Convert** → call `convert_model()` to produce `.tflite`
   - Print a final summary table across all horizons: target, rows, MAPE, MAE, RMSE, model size, pass/fail
   - Exit code: 0 if all pass, 1 if any fail quality gate

5. **Add `matplotlib` dependency**
   - Add `matplotlib==3.10.1` to root `requirements.txt` (needed for evaluation plots)

6. **Add unit tests**
   - `tests/test_train.py`:
     - Test `create_dataset_from_segments()` with synthetic data: correct window shape, segment boundary respect
     - Test model output shape matches `(batch, 1)` with a tiny 2-epoch train on synthetic data
     - Test that different `--target` values produce differently named artifacts
   - `tests/test_evaluate.py`:
     - Test MAPE/MAE/RMSE calculations against hand-computed known values
     - Test HPA comparison logic: given known RPS series, verify replica counts
   - `tests/test_convert.py`:
     - Test conversion of a small `.keras` model produces a valid `.tflite` file
     - Test quantized output is smaller than unquantized
   - All tests use synthetic data — no dependency on the real training CSV

**Verification**
- Run the full pipeline: `python model/pipeline.py --csv data-collection/training-data/training_data_v2.csv --epochs 10` (small epoch count for quick validation)
- Verify artifacts are created in `model/artifacts/`: 3 `.keras`, 3 `.tflite`, 3 `.pkl`, 3 plots, 3 JSON summaries
- Run tests: `pytest tests/test_train.py tests/test_evaluate.py tests/test_convert.py -v`
- Inspect evaluation plots visually for sanity

**Decisions**
- **70/20/10 split over k-fold**: Time-series data must be split chronologically, not shuffled. A simple temporal split is correct here.
- **One scaler per horizon**: Each horizon model gets its own scaler because the scaler is fit on training data which varies by split point — keeps artifacts self-contained.
- **Quality gate is a warning, not a hard block**: During early training with limited data (3 days), MAPE may be high. The pipeline reports it but still produces artifacts so you can iterate.
- **Operator changes deferred**: `predictor.py` currently only loads one model. Supporting multi-horizon inference in the operator is a separate task after the pipeline validates which horizon performs best.
