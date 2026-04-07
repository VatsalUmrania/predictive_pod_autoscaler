# PPA Python Package Structure Refactoring

**Status:** Proposal for phased implementation  
**Priority:** Medium (improves maintainability; preserve backward compatibility)  
**Timeline:** 3-4 incremental phases over multiple sprints

---

## Executive Summary

The current `src/ppa/` structure (score: 7.5/10) has a solid foundation but mixing infrastructure concerns (K8s, Prometheus) with domain logic reduces modularity and reusability. This proposal refactors via **safe incremental steps**—no big-bang rewrites—preserving all behavior while clarifying module boundaries.

**Key improvements:**
- **Extract domain logic** from operator/scaler.py and operator/features.py → new `domain/` module
- **Isolate infrastructure adapters** (K8s, Prometheus) into separate, swappable layers
- **Add explicit `__all__` APIs** for operator and CLI modules
- **Unify configuration** (merge dataflow/config.py into root config.py)
- **Remove unused code** (empty services/ and loader/ directories)
- Result: ML pipelines + scaling logic become **reusable outside K8s**, improve testability

---

## Current Issues

| Issue | Impact | Severity |
|-------|--------|----------|
| **Mixing domain + infrastructure in operator/** | Hard to test scaling math without K8s; reduces reusability | Medium |
| **No explicit `__all__` in operator, cli** | Unclear public APIs; harder for agents/IDEs to discover | Low |
| **Duplicated config** (dataflow/config.py vs ppa/config.py) | Source of truth unclear; sync issues | Medium |
| **CLI tightly coupled** to model.train/convert directly | CLI changes force model API changes; harder to refactor | Medium |
| **Empty services/, loader/ directories** | Code smell; confuses newcomers | Low |
| **Infrastructure (Prometheus, K8s) intertwined** | Hard to switch implementations (e.g., mock Prometheus for testing) | Medium |

---

## Recommended Target Structure

```
src/ppa/
├── __init__.py                          # Version + public API
├── config.py                            # ✓ UNIFIED config (no duplicates)
│
├── domain/                              # NEW: Pure domain logic
│   ├── __init__.py
│   ├── feature_validation.py            # Feature bounds checking (moved from operator/features.py)
│   ├── scaling.py                       # Replica calculation (moved from operator/scaler.py)
│   ├── state.py                         # CRState, drift detection
│   └── __all__ → [validate_features, calculate_replicas, CRState, ...]
│
├── common/                              # ✓ UNCHANGED: Shared constants
│   ├── __init__.py
│   ├── constants.py
│   ├── feature_spec.py
│   └── promql.py
│
├── model/                               # ✓ UNCHANGED: Pure ML
│   ├── __init__.py
│   ├── train.py
│   ├── convert.py
│   ├── evaluate.py
│   └── pipeline.py
│
├── infrastructure/                      # NEW: Adapter layer (K8s, Prometheus)
│   ├── __init__.py
│   ├── prometheus/
│   │   ├── __init__.py
│   │   ├── client.py                    # (existing operator/prometheus.py content)
│   │   └── circuit_breaker.py           # Extracted resilience logic
│   ├── kubernetes/
│   │   ├── __init__.py
│   │   ├── client.py                    # K8s client initialization (refactored)
│   │   └── scaler.py                    # K8s deployment patching (extracted from operator/scaler.py)
│   └── __all__ → [PrometheusClient, PrometheusCircuitBreaker, K8sClient, K8sScaler]
│
├── operator/                            # ✓ REFACTORED: Orchestration only
│   ├── __init__.py                      # Exports public reconciliation functions
│   ├── main.py                          # Timer handler, reconciliation coordinator
│   ├── reconciler.py                    # NEW: Reconciliation pipeline (features → predict → scale)
│   ├── features.py                      # KEEPS: Prometheus queries + circuit breaker resilience only
│   ├── predictor.py                     # ✓ UNCHANGED: TFLite inference wrapper
│   ├── retraining/
│   │   ├── __init__.py
│   │   ├── controller.py                # ✓ UNCHANGED
│   │   └── candidate.py                 # ✓ UNCHANGED
│   └── __all__ → [reconcile, build_feature_vector, predict, ...]
│
├── dataflow/                            # ✓ REFACTORED: Remove config duplication
│   ├── __init__.py
│   ├── export_training_data.py          # ✓ UNCHANGED
│   ├── validate_training_data.py        # ✓ UNCHANGED
│   └── verify_features.py               # ✓ UNCHANGED
│   # NOTE: dataflow/config.py → DELETED, merged into ppa/config.py
│
├── services/                            # NEW: High-level service layer
│   ├── __init__.py
│   ├── model_service.py                 # Unified interface to model.train, convert, evaluate
│   ├── scaling_service.py               # Unified replica calculation + infrastructure scaling
│   └── __all__ → [ModelService, ScalingService]
│
├── cli/                                 # ✓ REFACTORED: Cleaner imports
│   ├── __init__.py                      # Add explicit __all__
│   ├── app.py
│   ├── __main__.py
│   ├── commands/
│   │   └── (unchanged; now imports from services/ instead of model/)
│   ├── core/
│   └── utils.py
│
├── runtime/                             # ✓ UNCHANGED: Metrics export, health
│   ├── __init__.py
│   ├── health.py
│   ├── metrics.py
│   └── __all__ → [start_health_server, setup_metrics, ...]
│
└── [DELETE] services/ (old empty directory)
└── [DELETE] loader/ (Dockerfile belongs in root, not src/)
```

---

## Migration Plan: 4 Phases

### Phase 1: Foundation (Week 1-2) — Low Risk
**Goal:** Add explicit APIs, prepare for refactoring

1. **Add `__all__` to operator modules**
   - `operator/__init__.py`: `__all__ = ["reconcile", "build_feature_vector", "predict", ...]`
   - `operator/predictor.py`: `__all__ = ["predict", "PredictorState"]`
   - `operator/features.py`: `__all__ = ["build_feature_vector", "validate_feature_bounds"]`

2. **Unify config: Merge dataflow/config.py → ppa/config.py**
   - Copy dataflow-specific config classes to ppa/config.py
   - Update imports in dataflow/*.py: `from ppa.config import ...`
   - Delete dataflow/config.py

3. **Add `__all__` to cli/__init__.py**
   - Export main CLI app
   - Keep version string

4. **Delete empty directories**
   - Remove `src/ppa/services/` (prepare for new services layer)
   - Remove `src/ppa/loader/` (Dockerfile should be at project root)

5. **Run tests** to verify no breakage

---

### Phase 2: Extract Domain Logic (Week 3-4) — Medium Risk
**Goal:** Isolate pure domain logic from infrastructure

1. **Create `src/ppa/domain/` module**
   - `domain/__init__.py` with `__all__ = [...]`

2. **Extract feature validation (operator/features.py → domain/feature_validation.py)**
   - Move: `validate_feature_bounds()` + CircuitBreaker class
   - Keep in operator/features.py: `build_feature_vector()` (Prometheus-specific)
   - Create `domain/__init__.py`: `from .feature_validation import validate_feature_bounds`

3. **Extract scaling math (operator/scaler.py → domain/scaling.py)**
   - Move: `calculate_replicas()` (pure math)
   - Keep in infrastructure/kubernetes/scaler.py: `scale_deployment()` (K8s-specific)
   - Create `domain/__init__.py`: `from .scaling import calculate_replicas`

4. **Move state management (operator/main.py → domain/state.py)**
   - Move: `CRState` class definition + helpers
   - Keep operator/main.py: references to state management
   - Update imports: `from ppa.domain import CRState`

5. **Update operator imports**
   ```python
   # operator/features.py (now Prometheus-only)
   from ppa.domain import validate_feature_bounds
   
   # operator/main.py
   from ppa.domain import CRState, calculate_replicas
   ```

6. **Run unit tests** (should all pass; test_domain_*.py verify pure logic)

---

### Phase 3: Extract Infrastructure Adapters (Week 5-6) — Medium Risk
**Goal:** Isolate K8s and Prometheus interactions

1. **Create `src/ppa/infrastructure/` module**

2. **Move Prometheus logic**
   - Create: `infrastructure/prometheus/__init__.py`
   - Move: `operator/prometheus.py` → `infrastructure/prometheus/client.py`
   - Extract: `CircuitBreaker` from domain/feature_validation.py → `infrastructure/prometheus/circuit_breaker.py`
   - Update imports throughout

3. **Move K8s logic**
   - Create: `infrastructure/kubernetes/__init__.py`
   - Extract K8s-specific code from `operator/scaler.py` → `infrastructure/kubernetes/scaler.py`
   - Rename: `scale_deployment()` → `K8sScaler.patch_deployment()`

4. **Update operator/features.py**
   ```python
   # Instead of:
   # from ppa.operator.prometheus import ... (circular concern)
   
   # Now:
   from ppa.infrastructure.prometheus import PrometheusClient, CircuitBreaker
   ```

5. **Update operator/scaler.py** (now just a wrapper)
   ```python
   from ppa.infrastructure.kubernetes import K8sScaler
   from ppa.domain import calculate_replicas
   
   # Coordination logic
   ```

6. **Run integration tests** (operator tests may need mock updates)

---

### Phase 4: Service Layer & CLI Refactoring (Week 7-8) — Low Risk
**Goal:** Reduce coupling; enable service reuse

1. **Create `src/ppa/services/` module (replacement)**
   - `services/__init__.py` with `__all__ = [...]`

2. **Create ModelService** (src/ppa/services/model_service.py)
   ```python
   from ppa.model import train_model, convert_model, evaluate_model
   
   class ModelService:
       def train_and_convert(self, config) -> Tuple[Path, Metadata]:
           """High-level interface: train → convert → validate"""
           keras_model = train_model(...)
           tflite_model, metadata = convert_model(keras_model)
           return tflite_model, metadata
   ```

3. **Create ScalingService** (src/ppa/services/scaling_service.py)
   ```python
   from ppa.domain import calculate_replicas
   from ppa.infrastructure.kubernetes import K8sScaler
   
   class ScalingService:
       def apply_scaling_decision(self, cr, prediction):
           """High-level interface: calculate + apply"""
           replicas = calculate_replicas(prediction)
           self.k8s_scaler.patch_deployment(cr, replicas)
   ```

4. **Update CLI commands** to use services
   ```python
   # Before:
   from ppa.model.train import train_model
   
   # After:
   from ppa.services import ModelService
   service = ModelService()
   service.train_and_convert(...)
   ```

5. **Run E2E tests**

---

## Detailed File Operations

### Phase 1 Operations

```bash
# Add __all__ to existing files (no moves)
edit operator/__init__.py → add __all__
edit operator/predictor.py → add __all__
edit operator/features.py → add __all__
edit cli/__init__.py → add __all__

# Merge config
cat dataflow/config.py >> config.py  # (manual merge, preserve docstrings)
rm dataflow/config.py
update dataflow/*.py imports

# Cleanup
rmdir src/ppa/services/
rmdir src/ppa/loader/
```

### Phase 2 Operations

```bash
# Create domain module
mkdir -p src/ppa/domain
touch src/ppa/domain/__init__.py

# Extract feature validation
cat > src/ppa/domain/feature_validation.py << 'EOF'
# Move validate_feature_bounds() + CircuitBreaker from operator/features.py
# (See "Extract Scaling Math" example below)
EOF

# Extract scaling logic
cat > src/ppa/domain/scaling.py << 'EOF'
"""Pure domain logic for replica calculation (no K8s dependencies)."""

def calculate_replicas(
    forecast: float,
    current_replicas: int,
    config: ScalingConfig,
) -> int:
    """Calculate replicas from forecast. Pure math, testable independently."""
    # Existing calculate_replicas() logic from operator/scaler.py
    # (remove K8s API calls)
    return max(config.min_replicas, min(config.max_replicas, target))
EOF

# Extract state
cat > src/ppa/domain/state.py << 'EOF'
# Move CRState class definition from operator/main.py
EOF

# Update operator/features.py (remove moved content)
# Update operator/main.py (remove moved content, add imports)
# Update operator/scaler.py (remove moved content, add imports)
```

### Phase 3 Operations

```bash
# Create infrastructure module
mkdir -p src/ppa/infrastructure/{prometheus,kubernetes}
touch src/ppa/infrastructure/__init__.py
touch src/ppa/infrastructure/prometheus/__init__.py
touch src/ppa/infrastructure/kubernetes/__init__.py

# Move Prometheus client
mv src/ppa/operator/prometheus.py \
   src/ppa/infrastructure/prometheus/client.py

# Extract circuit breaker
cat > src/ppa/infrastructure/prometheus/circuit_breaker.py << 'EOF'
# Move CircuitBreaker class from domain/feature_validation.py
EOF

# Move K8s adapter
cat > src/ppa/infrastructure/kubernetes/scaler.py << 'EOF'
"""Kubernetes scaling adapter (infrastructure layer)."""
from kubernetes import client

class K8sScaler:
    def patch_deployment(self, cr, replicas):
        # Existing scale_deployment() logic from operator/scaler.py
        # (K8s API calls only)
        pass
EOF

# Update operator/features.py imports
# Update operator/scaler.py to use infrastructure layer
```

### Phase 4 Operations

```bash
# Create services module
mkdir -p src/ppa/services
touch src/ppa/services/__init__.py

# Create high-level services
cat > src/ppa/services/model_service.py << 'EOF'
"""Model training/conversion/evaluation orchestration."""
from ppa.model import train_model, convert_model, evaluate_model

class ModelService:
    def train_and_convert(self, config):
        keras_model = train_model(config)
        tflite_model, metadata = convert_model(keras_model)
        return tflite_model, metadata
EOF

cat > src/ppa/services/scaling_service.py << 'EOF'
"""Scaling decision combination and application."""
from ppa.domain import calculate_replicas
from ppa.infrastructure.kubernetes import K8sScaler

class ScalingService:
    def __init__(self, k8s_scaler: K8sScaler):
        self.k8s_scaler = k8s_scaler
    
    def apply_scaling_decision(self, cr, prediction):
        replicas = calculate_replicas(prediction, ...)
        self.k8s_scaler.patch_deployment(cr, replicas)
EOF

# Update cli/commands to use services
# Update cli/__init__.py to export services
```

---

## Benefits by Phase

| Phase | When Complete | Benefit |
|-------|---|---------|
| **Phase 1** | Week 2 | ✓ Config centralized; Unused code removed; clear public APIs |
| **Phase 2** | Week 4 | ✓ Scaling math testable without K8s; domain logic reusable |
| **Phase 3** | Week 6 | ✓ Infrastructure swappable (mock Prometheus for tests); clear adapter boundaries |
| **Phase 4** | Week 8 | ✓ CLI loosely coupled; services reusable in scripts/tools; ML pipeline independent |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Import changes break dependencies** | High | Test each phase thoroughly; keep old imports working via `__init__.py` re-exports for 1 sprint |
| **Circular imports introduced** | High | Run `pytest --collect-only` after each phase to verify acyclic imports |
| **Operator reconciliation breaks** | Critical | Run E2E tests with real K8s after Phase 3; mock Prometheus for safety |
| **CLI commands fail** | High | Run CLI integration tests after Phase 4; verify startup wizard works |
| **Performance regression** | Medium | Profile feature extraction + prediction latency before/after Phase 3 |
| **State management breaks** | Medium | Verify CRState serialization/deserialization in tests after Phase 2 |

---

## Backward Compatibility Strategy

**For each phase:**

1. Keep old imports working via re-exports in `__init__.py`:
   ```python
   # ppa/operator/__init__.py
   from ppa.domain import validate_feature_bounds
   from ppa.infrastructure.prometheus import PrometheusClient
   __all__ = [
       "validate_feature_bounds",     # For backward compat
       "PrometheusClient",
       ...
   ]
   ```

2. Add deprecation warnings (week 1 of next sprint, if needed):
   ```python
   import warnings
   warnings.warn(
       "ppa.operator.prometheus is deprecated; use ppa.infrastructure.prometheus",
       DeprecationWarning,
       stacklevel=2
   )
   ```

3. Remove old imports only after all dependents migrated (never break tests).

---

## Testing Strategy

### Phase 1
```bash
make test-unit           # All tests should pass (no code changes)
pytest tests/test_config.py -v  # Verify config merge
```

### Phase 2
```bash
pytest tests/unit/test_domain_*.py -v    # New domain tests (pure math, no K8s)
pytest tests/unit/test_feature_bounds.py -v
pytest tests/unit/test_scaler.py -v
make test-unit          # All unit tests must pass
```

### Phase 3
```bash
pytest tests/integration/ -v    # Operator + infrastructure integration
pytest tests/unit/ -v           # All unit tests
# No E2E yet—infrastructure adapters may not work with real K8s until Phase 4
```

### Phase 4
```bash
pytest tests/ -v                     # All tests
make test-e2e                        # Full end-to-end pipeline
# CLI integration tests via startup wizard
```

---

## Success Criteria

✅ Phase complete when:
1. All tests pass (unit + integration)
2. No new linting warnings (ruff, mypy)
3. Operator reconciliation works in test environment
4. CLI commands work end-to-end
5. Code coverage maintained or improved
6. Performance within 5% of baseline (no regression)

---

## Post-Refactoring Benefits

Once complete, the project gains:

| Benefit | Use Case |
|---------|----------|
| **Pluggable infrastructure** | Swap Prometheus for CloudWatch or InfluxDB; swap K8s for Nomad or Slurm |
| **Offline ML pipelines** | Train/evaluate models without K8s cluster; deploy anywhere |
| **Improved testability** | Mock infrastructure easily; test domain logic without external dependencies |
| **Agent productivity** | Clear module boundaries lead to better code completion + documentation discovery |
| **Smaller Docker images** | Infrastructure adapters can be optional dependencies (lite vs. full operator image) |
| **Reusable services** | Use ModelService + ScalingService in CLI, dashboards, or custom tooling |

---

## Decision Log

**Kept as-is (no refactoring needed):**
- ✓ `common/` — Pure constants, already well-organized
- ✓ `model/` — Pure ML, no external dependencies
- ✓ `operator/predictor.py` — Clean TFLite wrapper
- ✓ `dataflow/` (logic only; config merged) — Data collection, focused
- ✓ `cli/core/` and `cli/utils.py` — CLI infrastructure helpers

**Scheduled for refactoring:**
- ⚠️ `operator/scaler.py` — Extract domain logic + K8s adapter
- ⚠️ `operator/features.py` — Keep Prometheus; extract feature validation
- ⚠️ `operator/main.py` — Coordinator + state management

**New modules added:**
- 🆕 `domain/` — Pure scaling/validation logic
- 🆕 `infrastructure/` — K8s and Prometheus adapters
- 🆕 `services/` — High-level orchestration layer

---

## Rollback Strategy

If issues arise at any phase:

1. **Revert commits** for the current phase (git revert)
2. **Restore from backup** (git stash, previous branch)
3. **Run tests** to verify rollback successful
4. **Post-mortem** on what went wrong; adjust plan before retry

Example: Phase 3 breaks E2E tests
```bash
# Quickly rollback
git revert HEAD~5..HEAD  # Revert last 5 commits of Phase 3
make test-e2e            # Verify regression fixed
# Plan: Review K8s adapter impl, improve mock, retry
```

---

## Next Steps

1. **Get stakeholder approval** on target structure (this document)
2. **Schedule Phase 1** for next sprint
3. **Create tracking issues** for each phase with test coverage requirements
4. **Set up CI/CD hooks** to catch import cycles, coverage regressions
5. **Document progress** in project wiki as phases complete

---

## Related Documentation

- [docs/DEVELOPMENT.md](../../docs/DEVELOPMENT.md) — Current structure reference
- [docs/architecture.md](../../docs/architecture.md) — System context
- [docs/operator/architecture.md](../../docs/operator/architecture.md) — Operator reconciliation details
- `.github/copilot-instructions.md` — Agent guidelines (will reference this post-refactoring)
