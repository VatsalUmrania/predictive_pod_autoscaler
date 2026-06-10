# CLAUDE.md

> **PPA + NEXUS** — Kubernetes predictive autoscaling + self-healing infrastructure  
> **Full docs:** [docs/](docs/) | **Start:** [docs/INDEX.md](docs/INDEX.md)

---

## Project

Monorepo with two systems:
- **PPA** — LSTM-based traffic prediction, proactive pod scaling
- **NEXUS** — 7 agents detect failures, LLM-powered RCA, governed healing (L0-L3)

**Integration:** PPA = NEXUS Predictive Plane. NEXUS governs PPA scaling via ActionLadder.

---

## Structure

```
src/ppa/         # PPA: operator (Kopf), ML (TFLite), Prometheus metrics
src/nexus/       # NEXUS: agents, RCA, governance, NATS event bus
deploy/          # K8s manifests
tests/           # Unit + integration tests
model/           # Pre-trained .tflite models
```

---

## Quick Commands

```bash
pip install -e ".[dev]"         # Install
pytest tests/unit -v            # Unit tests (fast)
pytest tests/integration -v     # Integration (needs K8s)

paa operator build|deploy       # PPA ops
nexus server start              # NEXUS ops
```

---

## Key Files

| Purpose | File |
|---------|------|
| Operator reconciliation | `src/ppa/operator/main.py` |
| Configuration | `src/ppa/config.py` |
| Agent base class | `src/nexus/agents/base_agent.py` |
| RCA engine | `src/nexus/reasoning/rca_engine.py` |

---

## Configuration

Single source: `src/ppa/config.py`

```bash
PPA_PROMETHEUS_URL=http://prometheus:9090
PPA_MODEL_DIR=/models
NEXUS_NATS_URL=nats://localhost:4222
```

---

## Guidelines

- Imports: `from ppa.config import ...`
- Tests: `tests/unit/` (mock external deps)
- Lint: `ruff check src tests`
- Format: `black src tests`

---

## Get Help

1. Architecture → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
2. PPA details → [`docs/PPA_ARCHITECTURE.md`](docs/PPA_ARCHITECTURE.md)
3. NEXUS details → [`docs/NEXUS_ARCHITECTURE.md`](docs/NEXUS_ARCHITECTURE.md)
4. Operations → [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
5. Dev guide → [`docs/DEVELOPER.md`](docs/DEVELOPER.md)
