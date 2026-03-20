# Migration Guide — March 2026 Structural Changes

This document captures the directory restructuring done in March 2026. If you're returning to the codebase after this date, read this first.

---

## What Changed

The codebase was reorganized into a clean `src/ppa/` package structure:

### Directory Moves

| Old Location | New Location | Purpose |
|---|---|---|
| `data-collection/` | `data/` | Training data, test-app source |
| `data-collection/training-data/` | `data/training-data/` | Prometheus-exported CSVs |
| `model/artifacts/` | `data/artifacts/` | Trained .keras models, scalers, TFLite |
| `model/champions/` | `data/champions/` | Production-approved models |

### Python Import Root

All code now lives under `src/ppa/`. Use the package import pattern:

```python
# Old (no longer works)
from model.train import train_model
from common.promql import prom_range_query
from cli.config import NAMESPACE

# New
from ppa.model.train import train_model
from ppa.common.promql import prom_range_query
from ppa.config import NAMESPACE
```

### Configuration

`src/ppa/config.py` is now the **single source of truth** for all configuration. All three legacy config files (`cli/config.py`, `operator/config.py`, `dataflow/config.py`) are deprecated wrappers that re-export from `ppa.config`.

```python
# New canonical import
from ppa.config import get_config, Config, OperatorConfig, ModelConfig, ScalingConfig
```

### CLI Commands

ML commands use the `ppa model` subcommand:

```bash
# Old
python model/pipeline.py --csv data-collection/training-data/training_data_v2.csv
python model/train.py --target rps_t10m

# New
ppa model pipeline --csv data/training-data/training_data_v2.csv
ppa model train --target rps_t10m
```

Data collection uses `ppa data`:

```bash
ppa data export --hours 168 --step 15s
ppa data validate data/training-data/training_data_v2.csv
python -m ppa.dataflow.verify_features
```

### Why `data/` Not `model/`?

The `data/` directory holds runtime artifacts generated during operation — training CSVs, model checkpoints, champions, and the test-app. `model/` at the project root was a source directory (Python modules), not a data directory, so `data/` is the appropriate name for the runtime artifact store.

### Backward Compatibility

Deprecated config wrappers exist but emit `DeprecationWarning`:

```python
from ppa.cli.config import NAMESPACE  # Works, but emits warning
```

Migrate to `from ppa.config import NAMESPACE` to silence the warning.

### Dockerfiles

Build context changed from `PROJECT_DIR/src` to `PROJECT_DIR`:

```dockerfile
# Old (inside src/ppa/dataflow/Dockerfile)
COPY src/ppa/common/ src/ppa/common/

# New
COPY src/ppa/common/ src/ppa/common/
# Build: docker build -f src/ppa/dataflow/Dockerfile -t ppa-data-collector:latest .
```

### Kubernetes Manifests

All manifests remain in `deploy/`. The CronJob PVC references `ppa-training-data-pvc` which writes to the `data/training-data/` path inside the PVC.
