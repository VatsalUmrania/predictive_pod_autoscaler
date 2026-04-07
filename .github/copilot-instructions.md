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

## Architecture Overview

**PPA** is a Kubernetes operator that forecasts load 3–10 minutes ahead using LSTM neural networks and scales proactively. See [docs/architecture.md](../../docs/architecture.md) for the full system design.

**Core modules** in `src/ppa/`:
- **`cli/`** – Typer-based command groups; interactive startup wizard and GitOps deployment flows
- **`operator/`** – Kopf-based Kubernetes operator: reconciliation loop, Prometheus feature extraction, TFLite inference, scaling decisions
- **`model/`** – ML pipeline: LSTM training, TFLite conversion with quantization validation, accuracy evaluation
- **`dataflow/`** – Prometheus metric extraction and training data validation
- **`config.py`** – Single source of truth for all configuration (dataclasses, env-var driven)
- **`common/`, `runtime/`** – Shared utilities and health/metrics endpoints

For detailed operator architecture, see [docs/operator/architecture.md](../../docs/operator/architecture.md).

---

## Code Style & Conventions

See [docs/DEVELOPMENT.md](../../docs/DEVELOPMENT.md) for comprehensive guidelines.

**Essentials:**
- **Line length:** 100 chars (Black + Ruff)
- **Type hints:** Required for all functions and public APIs (mypy config: `ignore_missing_imports=true`)
- **Docstrings:** Triple-quote format; concise with examples for complex functions
- **Naming:** `snake_case` functions/variables, `PascalCase` classes, `_private` prefix for internals
- **Config:** Never hardcode—always use `ppa.config` (env vars, .env files, CLI overrides)
- **Logging:** Python `logging` module; Kopf captures operator logs automatically; use `console.print()` for CLI output

---

## Testing Essentials

See [docs/DEVELOPMENT.md#testing-strategy](../../docs/DEVELOPMENT.md) for full strategy.

**Quick commands:**
```bash
make test-unit          # Fast unit tests (mocked Prometheus/K8s)
make test-integration   # Component interaction tests
make test-e2e          # Full system tests (slower)
```

**Key patterns:**
- All tests mock external dependencies (use fixtures in `conftest.py`)
- Tests live in `tests/{unit,integration,e2e}/`
- Use `test_config` fixture for consistent test environment
- All test functions start with `test_`

---

## Critical Design Patterns

**When working on these areas, apply these patterns:**

1. **Configuration-First:** All config in `config.py` as dataclasses, loaded from env vars or `.env`. Never hardcode constants.

2. **Prometheus Circuit Breaker (PR#11):** Metrics failures tracked per-CR; after 10 consecutive failures, circuit opens for 60s. Check `features.py:_get_circuit_breaker()`.

3. **Feature Bounds Validation (PR#12):** Pre-prediction validation checks for NaN, infinity, and out-of-range values to catch data quality issues early. See `validate_feature_bounds()`.

4. **Quantization Validation (PR#8):** Model conversion validates quantized accuracy loss < 5%; deployment fails if exceeded. Check `model/convert.py`.

5. **State Management (PR#10):** Per-CR state persists in Kopf's patched `status` field. Enables multi-pod deployments without shared storage. Use `CRState` class.

6. **Rate-Limited Scaling:** Apply 2-step smoothing + gradient clipping (2x up, 0.5x down) to prevent flapping. See `scaler.py`.

---

## Common Tasks

### Adding a New CLI Command
1. Create `src/ppa/cli/commands/mycommand.py` with a Typer app
2. Register in `src/ppa/cli/app.py`: `app.add_typer(mycommand_app, name="mycommand")`
3. Use `console.print()` for output (rich formatting)

### Adding a Prometheus Metric to Feature Vector
1. Add query to `build_feature_vector()` in `src/ppa/operator/features.py`
2. Add bounds check in `validate_feature_bounds()` (check for NaN, reasonable range)
3. Add test in `tests/unit/test_feature_spec.py`

### Modifying ML Model
1. Update training logic in `src/ppa/model/train.py`
2. Run `pytest tests/test_train.py -v` to verify
3. Update model conversion validation if quantization threshold changes

### Training a New Model
```bash
ppa model train --config=config.yaml --output=my_model.keras
ppa model convert --input=my_model.keras --output=my_model.tflite
ppa model evaluate --model=my_model.tflite --test-data=data.csv
```

### Deploying Operator
See [docs/operator/deployment.md](../../docs/operator/deployment.md) for step-by-step deployment.

---

## Troubleshooting

See [docs/operator/troubleshooting.md](../../docs/operator/troubleshooting.md) for comprehensive troubleshooting guide.

**Quick diagnostics:**
- **Operator not reconciling?** Check logs: `kubectl logs -f deployment/ppa-operator -n ppa-system`
- **High prediction latency?** Profile feature extraction: check circuit breaker state and Prometheus query performance
- **Model accuracy degrading?** Check [docs/technical-debt/](../../docs/technical-debt/) for concept drift detection and retraining logic
- **Scaling decisions inappropriate?** Verify feature bounds validation in operator logs; check scaler rate-limiting config

**Health check:** `curl http://localhost:8080/healthz` (operator health endpoint)

---

## Observability & Metrics

The operator exports Prometheus metrics on `:9100/metrics` with `predictive_autoscaler` prefix. Key metrics:
- `ppa_replicas_current` – Current replica count per CR
- `ppa_prediction_latency_ms` – Feature extraction + inference time
- `ppa_circuit_breaker_open` – Circuit breaker state (1 = open, 0 = closed)

To add metrics:
1. Use Python `prometheus_client` library (already imported in `runtime/metrics.py`)
2. Register metrics in `runtime/metrics.py`
3. Reference in operator code via `from ppa.runtime.metrics import metric_name`

See [docs/operator/api.md](../../docs/operator/api.md) for complete metrics reference.

---

## Documentation Index

- [Architecture Overview](../../docs/architecture.md)
- [Operator Guide](../../docs/operator/README.md) – Deployment, configuration, API, troubleshooting
- [Development Guide](../../docs/DEVELOPMENT.md) – Setup, testing, code quality
- [ML Pipeline Reference](../../docs/reference/ml_commands.md)
- [Technical Debt Status](../../docs/technical-debt/00-STATUS.md) – Production safety metrics (64% complete)
- [Production Roadmap](../../docs/production_roadmap.md) – Future phases and scaling plans
