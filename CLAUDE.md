# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **monorepo** containing two complementary Kubernetes autoscaling systems:

1. **PPA (Predictive Pod Autoscaler)** - ML-driven horizontal pod autoscaler using LSTM neural networks to forecast traffic 3-10 minutes ahead
2. **NEXUS** - Self-healing infrastructure layer built on PPA that adds proactive failure detection, automated healing, and incident response

## Project Structure

```
├── src/
│   ├── ppa/              # Predictive Pod Autoscaler core
│   │   ├── cli/           # PPA CLI implementation (ppa command)
│   │   ├── operator/      # Kopf-based Kubernetes operator
│   │   ├── model/         # ML training and TFLite conversion
│   │   ├── dataflow/      # Traffic generation and metric collection
│   │   └── common/        # Shared utilities
│   └── nexus/             # NEXUS self-healing system
│       ├── agents/        # Specialized healing agents
│       ├── cli/           # NEXUS CLI implementation
│       ├── predictive/    # Predictive scaling enhancements
│       ├── reasoning/     # RCA engine and LLM integration
│       ├── governance/    # Policy enforcement
│       └── sdk/           # SDK for app integration
├── deploy/                # Kubernetes manifests
├── tests/                 # Test suites
├── demo/                  # Example applications
├── model/                 # Pre-trained ML models (.tflite)
└── data/                  # Training datasets and SQLite audit DB
```

## Common Development Commands

**Installation:**
```bash
# Standard install
pip install -e .

# Development install with all extras
pip install -e ".[dev]"
pip install -e ".[nexus,llm]"  # For NEXUS development
```

**Testing:**
```bash
# Unit tests (fast)
make test-unit
pytest tests/unit -v

# Integration tests
make test-integration
pytest tests/integration -v

# Full test suite
make test

# Specific test file
pytest tests/unit/test_cli.py -v
pytest tests/unit/test_cli.py::test_specific_function -v
```

**Code Quality:**
```bash
# Lint and typecheck
make lint
make typecheck

# Auto-format code
make format

# Individual tools
ruff check src tests
mypy src/ppa --ignore-missing-imports
black src tests
```

**Building & Deployment:**
```bash
# Build operator Docker image
ppa operator build

# Deploy to current kubectl context
ppa operator deploy

# Check operator status
ppa operator status

# Stream operator logs
ppa operator logs
```

## CLI Tools

**PPA CLI (`ppa` command):**
- `ppa operator build` - Build Docker image
- `ppa operator deploy` - Deploy operator
- `ppa operator status` - Check status
- `ppa operator logs` - View logs
- `ppa train` - Train custom ML model
- `ppa add` - Add/annotate applications
- `ppa push` - Publish metrics

**NEXUS CLI (`nexus` command):**
- `nexus server start` - Start NEXUS API server
- `nexus operator start` - Start NEXUS Kubernetes operator
- `nexus dashboard` - Open developer dashboard
- `nexus apps register` - Register new applications

Run either CLI with no arguments to enter interactive shell mode with auto-completion.

## Architecture Patterns

**Kubernetes Operator Pattern:**
- Uses Kopf framework for managing PredictiveAutoscaler CRDs
- Timer-based reconciliation loops (every 30-60 seconds)
- Prometheus queries for metrics extraction
- TFLite models for inference (<100ms prediction time)

**ML Pipeline:**
```
Historical Metrics (Prometheus) → Feature Extraction → LSTM Model
                                    ↓
Prediction → Scale Decision → K8s Deployment Patch
```

**NEXUS Event Flow:**
```
Application Instrumentation → NATS Event Bus → Healing Agent
                                ↓
                        Audit Database (SQLite)
                                ↓
                        RCA Engine (Gemini/OpenAI/Anthropic)
                                ↓
                        Action Execution → K8s API
```

**Key Components:**
- **Feature Engine**: Extracts 14 features from Prometheus metrics
- **Predictor**: Runs TFLite models for scaled replica predictions
- **Healing Agents**: Specialized agents for different failure types (OOM, crashes, config errors)
- **RCA Engine**: Uses LLMs to analyze incident patterns and suggest fixes

## Configuration

**Environment Variables:**
- `PPA_PROMETHEUS_URL` - Prometheus endpoint (default: `http://prometheus:9090`)
- `PPA_MODEL_PATH` - Directory containing .tflite models (default: `/models`)
- `PPA_LOG_LEVEL` - Logging verbosity (default: `INFO`)
- `NEXUS_API_URL` - NEXUS server URL for SDK
- `SELFHEAL_TOKEN` - Authentication token for NEXUS SDK
- `NEXUS_GEMINI_API_KEY` - For AI-powered RCA (optional, falls back to rule-based)

**PredictiveAutoscaler CRD:**
```yaml
apiVersion: autoscaling.ppa.io/v1alpha1
kind: PredictiveAutoscaler
metadata:
  name: my-app
spec:
  targetRef:
    apiGroup: apps
    kind: Deployment
    name: my-app
  modelId: "lstm-rps-v2"
  minReplicas: 2
  maxReplicas: 50
  lookAheadMinutes: 5
```

## Development Notes

**Adding New Features:**
- CLI commands go in `src/ppa/cli/commands/` or `src/nexus/cli/commands/`
- Operator logic goes in `src/ppa/operator/` or `src/nexus/operator/`
- ML models are trained separately and saved as .tflite files in `model/`
- New healing agents go in `src/nexus/agents/`
- Shared code between PPA and NEXUS goes in `src/shared/` (if needed)

**Model Training:**
Models are trained offline using historical Prometheus data. The pipeline:
1. Collect traffic patterns via Locust generators
2. Extract features (RPS, latency, CPU, memory, temporal patterns)
3. Train LSTM networks on sliding windows
4. Convert to TensorFlow Lite for production inference

**Testing Approach:**
- Unit tests for individual functions
- Integration tests for operator behavior
- E2E tests for complete scaling workflows
- Use `kopf.testing` for operator testing

**LLM Integration:**
- Default RCA uses rule-based logic
- Set `NEXUS_GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` for AI-powered RCA
- LLM provider selected via `get_llm_provider()` in `src/nexus/reasoning/rca.py`

## Key Files for Quick Reference

- `src/ppa/operator/main.py` - Core operator reconciliation loop
- `src/ppa/config.py` - Feature definitions and PromQL queries
- `src/nexus/cli/main.py` - NEXUS CLI entry point
- `deploy/predictiveautoscaler.yaml` - Example PPA policy
- `dev-guide/DEVELOPER_GUIDE.md` - NEXUS integration guide
- `demo/selfheal.yaml` - Example NEXUS configuration
