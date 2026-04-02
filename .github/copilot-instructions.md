# Copilot Instructions for Predictive Pod Autoscaler (PPA)

## Quick Reference

**Python Version:** 3.10+ | **Package Manager:** pip with setuptools

### Build & Install
```bash
pip install -e .                    # Development mode (editable)
pip install -e ".[dev]"             # With development dependencies
```

### Test Commands
```bash
make test                           # Run unit tests (fastest)
make test-unit                      # Unit tests only
make test-integration               # Integration tests
make test-e2e                       # End-to-end tests
pytest tests/unit -v -k test_name   # Single test by name
pytest tests/unit -v --tb=short     # With minimal traceback
```

### Lint, Format & Type Checking
```bash
make lint                           # Ruff + mypy (8 errors stops)
make format                         # Black + ruff auto-fix
make typecheck                      # mypy only
ruff check src tests --fix          # Fix fixable ruff violations
```

### Clean Build Cache
```bash
make clean                          # Remove __pycache__, .pytest_cache, .mypy_cache, build artifacts
```

---

## High-Level Architecture

**PPA** is a Kubernetes operator that forecasts load 3–10 minutes ahead using LSTM neural networks and scales proactively (vs. reactive HPA).

### Core Modules

- **`cli/`** – Typer-based command groups (`startup`, `deploy`, `operator`, `model`, `data`, `monitor`, etc.). Each command group is its own Typer app, imported in `cli/app.py`.

- **`operator/`** – Kopf-based Kubernetes operator (watches `PredictiveAutoscaler` CRDs):
  - `main.py` – Reconciliation loop, health server, state management
  - `features.py` – Prometheus metric queries (with circuit breaker for resilience) → feature vector
  - `predictor.py` – Loads TFLite models, makes predictions
  - `scaler.py` – Calculates replica count from forecast
  - `retraining/` – Model retraining logic

- **`model/`** – ML pipeline:
  - `train.py` – Trains LSTM models on historical data
  - `convert.py` – Keras → TFLite conversion with quantization validation
  - `evaluate.py` – Model accuracy metrics
  - `pipeline.py` – End-to-end training pipeline

- **`dataflow/`** – Data collection & validation for training

- **`common/`** – Shared utilities (logging, errors, helpers)

- **`config.py`** – Centralized config (dataclasses for Prometheus, Operator, Model, Scaling, etc.)

- **`runtime/`** – Runtime utilities (health checks, metrics)

### Key Design Patterns

1. **Configuration-First:** All config lives in `config.py` as dataclasses. Loaded from env vars or `.env` files (via python-dotenv).

2. **Error Handling with Circuit Breakers:** Prometheus metric failures trigger circuit breaker (PR#11). Tracks failures → fallback after threshold.

3. **Feature Vector Validation:** `validate_feature_bounds()` checks for NaN, infinity, and bounds to catch concept drift early (PR#12).

4. **Quantization Validation:** Model conversion validates quantized model accuracy loss < 5%, fails if exceeded (PR#8).

5. **State Management:** Kopf operator stores per-CRD state (feature history, circuit breaker state) to enable retraining decisions and fault tolerance.

---

## Key Conventions

### Python Style
- **Line length:** 100 chars (configured in `pyproject.toml`)
- **Formatter:** Black
- **Linter:** Ruff (select E, F, W, I, N, UP, B; ignore E501)
- **Type Checking:** mypy with `ignore_missing_imports=true`
- **Docstrings:** Use triple-quote format; keep concise. Include examples in complex functions.
- **Naming:** snake_case for functions/variables, PascalCase for classes. Private functions start with `_`.

### Testing
- Tests live in `tests/unit/`, `tests/integration/`, `tests/e2e/`
- Use pytest fixtures from `conftest.py` (`test_config`, mock Prometheus, etc.)
- Mock Kubernetes/Prometheus calls (no live cluster/prometheus in tests)
- Fixture prefix: all test functions start with `test_`

### Imports & Modules
- Use relative imports within the same module hierarchy only when necessary; prefer absolute imports from `ppa.*`
- CLI commands use Typer for type-safe argument parsing and rich formatting
- Kubernetes client: use `kubernetes.client` via Kopf integration

### Error Handling
- Custom exceptions in `config.py` for application-level errors (`FeatureVectorException`, `PrometheusCircuitBreakerError`)
- Use `console` from `ppa.cli.utils` for user-facing output (rich formatting)
- Kopf logs are captured automatically; use Python logger for additional instrumentation

### Operator Lifecycle
- Reconciliation is triggered by CRD changes or timer events (every `TIMER_INTERVAL` seconds)
- Stateful logic stored in `patch['status']` for persistence across reconciliation cycles
- Use `CRState` class to manage state objects (feature history, circuit breaker state)

### Model & ML
- Models are TFLite format (quantized, ~2–5MB footprint)
- Feature vectors are timestamped dicts with keys like `rps_t1m`, `cpu_percent`, etc.
- Training expects CSV data with timestamps + metrics in `dataflow/`

---

## Testing Strategy

### Unit Tests (`tests/unit/`)
Fast, isolated tests for individual functions/classes. Mock all external dependencies (Prometheus, Kubernetes, filesystem).

**Run:** `make test-unit` or `pytest tests/unit -v`

**Example:**
```python
def test_validate_feature_bounds_detects_nan(test_config):
    features = {"rps_t1m": float("nan")}
    cleaned, warnings = validate_feature_bounds(features)
    assert len(warnings) > 0
    assert cleaned["rps_t1m"] is None
```

### Integration Tests (`tests/integration/`)
Test interactions between modules (e.g., config → model → predictor) without live K8s/Prometheus.

**Run:** `make test-integration` or `pytest tests/integration -v`

### E2E Tests (`tests/e2e/`)
Full pipeline tests with a real or mocked Kubernetes cluster. Less frequent, slower.

**Run:** `make test-e2e` or `pytest tests/e2e -v`

---

## Common Tasks

### Adding a New CLI Command
1. Create `src/ppa/cli/commands/mycommand.py` with a Typer app
2. Import and register in `src/ppa/cli/app.py`:
   ```python
   from ppa.cli.commands.mycommand import app as mycommand_app
   app.add_typer(mycommand_app, name="mycommand")
   ```
3. Use `console.print()` for output (rich formatting)

### Adding a Prometheus Metric to Feature Vector
1. Add query to `build_feature_vector()` in `src/ppa/operator/features.py`
2. Validate bounds in `validate_feature_bounds()` (check for NaN, reasonable range)
3. Add test in `tests/unit/test_feature_spec.py`

### Modifying ML Model
1. Update training logic in `src/ppa/model/train.py`
2. Run `pytest tests/test_train.py -v` to verify
3. Update model conversion validation if needed (check quantization loss threshold)

### Deploying Operator
1. Build Docker image: `ppa operator build`
2. Deploy to cluster: `ppa operator deploy`
3. Verify: `ppa operator status`
4. Inspect logs: `ppa operator logs`

---

## Notes for Future Sessions

- **Prometheus Circuit Breaker:** `_get_circuit_breaker()` and `_set_circuit_breaker()` track consecutive Prometheus failures. After `failure_threshold` (default 10), circuit opens and metric queries are skipped for 60s. Check `features.py`.

- **Feature Bounds Validation:** Always validate feature vectors before prediction to catch data quality issues early. See `validate_feature_bounds()` and test cases in `test_pr11_feature_bounds.py`.

- **Quantization:** Models are converted to TFLite with int8 quantization. Accuracy loss must be < 5% or deployment fails. Check `model/convert.py`.

- **State Management:** Operator state persists in CRD status. Access via `status` param in reconcile function. Enables multi-pod operator deployments without shared storage.

- **Configuration:** Never hardcode config—always use `ppa.config` module. Supports env vars, `.env` files, and CLI overrides.

- **Testing Gotchas:**
  - Always mock Kubernetes API (use fixtures in `conftest.py`)
  - Always mock Prometheus (return test data, not real metrics)
  - Use `test_config` fixture for consistent test environment

- **Logging:** Use Python's built-in `logging` module. Logs are captured by Kopf in operator context; use `console.print()` for CLI output.
