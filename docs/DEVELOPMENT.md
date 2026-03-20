# Development Guide

## Quick Start

### Setup

```bash
git clone <repo>
cd ppa
make install-dev
```

### Running Tests

```bash
# Unit tests only (fastest, ~1 minute)
make test

# By category
make test-unit          # Fast unit tests
make test-integration   # Component interaction tests
make test-e2e           # Full system tests (slow, ~5 mins)

# All tests
pytest tests/
```

### Code Quality

```bash
# Check for issues
make lint

# Auto-fix code style
make format

# Strict type checking
make typecheck
```

## Project Structure

```
src/ppa/                    # All application code (canonical import root)
├── __init__.py            # Version, public API
├── config.py              # ✅ Centralized configuration (single source of truth)
├── common/                # Shared utilities
│   ├── constants.py
│   ├── feature_spec.py
│   └── promql.py
├── operator/              # Kubernetes operator
│   ├── main.py
│   ├── features.py
│   ├── predictor.py
│   └── scaler.py
├── model/                 # ML training & inference
│   ├── train.py
│   ├── evaluate.py
│   ├── pipeline.py
│   └── convert.py
├── dataflow/              # Metrics collection
│   ├── export_training_data.py
│   ├── validate_training_data.py
│   └── verify_features.py
└── cli/                   # Command-line interface
    └── commands/

tests/                      # Test suite (organized by type)
├── conftest.py            # Shared fixtures
├── fixtures/              # Test data & mocks
├── unit/                  # Fast unit tests
├── integration/           # Component tests
└── e2e/                   # Full system tests

data/                       # Runtime data (NOT in VCS — created at runtime)
├── training-data/         # Prometheus-exported CSVs for model training
├── artifacts/             # Trained .keras models, scalers, TFLite models
├── champions/             # Production-approved model sets
└── test-app/             # Instrumented test application source

deploy/                     # Kubernetes manifests
docs/                       # Documentation (this directory)
```

## Configuration

All configuration is centralized in `src/ppa/config.py`:

```python
from ppa.config import get_config

config = get_config()
print(config.prometheus.url)
print(config.operator.namespace)
```

### Environment Variables

```bash
# Prometheus
PROMETHEUS_URL=http://localhost:9090
PROM_TIMEOUT=2
PPA_PROM_FAILURE_THRESHOLD=10

# Operator
PPA_NAMESPACE=default
PPA_TIMER_INTERVAL=30
PPA_INITIAL_DELAY=60
PPA_STABILIZATION_STEPS=2
PPA_STABILIZATION_TOLERANCE=0.5
PPA_LOOKBACK_STEPS=60
LOG_LEVEL=INFO

# Model
PPA_MODEL_DIR=/models
PPA_DEFAULT_HORIZON=rps_t10m

# Scaling
PPA_MIN_REPLICAS=2
PPA_MAX_REPLICAS=20
PPA_SCALE_UP_RATE=2.0
PPA_SCALE_DOWN_RATE=0.5
PPA_CAPACITY_PER_POD=50
PPA_SAFETY_FACTOR=1.10

# Data Collection
TARGET_APP=test-app
NAMESPACE=default
CONTAINER_NAME=test-app

# CLI
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
APP_PORT=8080
```

## Common Workflows

### Add a New Test

1. Create test in appropriate directory:
   - `tests/unit/test_*.py` for unit tests
   - `tests/integration/test_*.py` for component tests
   - `tests/e2e/test_*.py` for full system tests

2. Imports:
   ```python
   from ppa.config import get_config, set_config
   from ppa.operator.features import extract_features
   # etc.
   ```

3. Use fixtures from `conftest.py`:
   ```python
   def test_something(test_config, mock_prometheus_response):
       set_config(test_config)
       # ...
   ```

### Fix a Failing Test

1. Run the specific test:
   ```bash
   pytest tests/unit/test_foo.py::TestClass::test_method -v
   ```

2. Check imports are correct (should be `from ppa.*`)

3. Use `set_config()` in tests to inject test configs

### Add a New Feature

1. Create code in appropriate `src/ppa/*/` module
2. Add tests in `tests/unit/` and `tests/integration/`
3. Update docstrings and type hints
4. Run `make lint format test`
5. Update relevant docs in `docs/`

## Debugging

### Enable debug logging

```bash
LOG_LEVEL=DEBUG ppa <command>
```

### Run Python REPL with config

```bash
python -c "from ppa.config import get_config; c = get_config(); print(c)"
```

### Check imports work

```bash
python -c "from ppa.operator.features import extract_features; print('OK')"
```

## Troubleshooting

**Import errors like `No module named 'ppa.X'`?**
- Run `make install-dev` to install the package in editable mode
- Ensure your shell/editor uses the right Python interpreter

**Tests fail with config not found?**
- Ensure `src/ppa/config.py` exists and has the right structure
- Tests should use the `test_config` fixture from `conftest.py`

**Linting errors?**
- Run `make format` to auto-fix most issues
- `make lint` will show remaining issues

## Useful Commands

```bash
# Run one test file
pytest tests/unit/test_foo.py -v

# Run tests matching a pattern
pytest -k "test_feature" -v

# Run with coverage
pytest --cov=src/ppa --cov-report=html

# Run with extra verbosity
pytest -vv --tb=long
```
