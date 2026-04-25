<div align="center">

# NEXUS
### Self-Healing Infrastructure for Kubernetes

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.28%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Prometheus](https://img.shields.io/badge/Prometheus-Monitored-E6522C?logo=prometheus&logoColor=white)](https://prometheus.io)
[![Grafana](https://img.shields.io/badge/Grafana-Dashboard-F46800?logo=grafana&logoColor=white)](https://grafana.com)
[![NATS](https://img.shields.io/badge/NATS-JetStream-27AAE1?logo=natsdotio&logoColor=white)](https://nats.io)
[![License](https://img.shields.io/badge/License-MIT-22C55E)](./LICENSE)

**NEXUS** moves Kubernetes self-healing from _reactive_ to _proactive_ — detecting failures before they surface, healing autonomously at the right blast-radius, and learning from every incident to get better over time.

</div>

---

## Table of Contents

- [What NEXUS Does](#what-nexus-does)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Grafana Dashboard](#grafana-dashboard)
- [Development Guide](#development-guide)
- [Phase Roadmap](#phase-roadmap)

---

## What NEXUS Does

Traditional Kubernetes self-healing waits for a pod to crash before reacting. NEXUS instruments everything — load balancers, git events, pod metrics, DB query patterns — and acts **before** failures cascade.

```
DB sees ORDER table spike  →  Predicts /api/orders traffic  →  Pre-scales orders-api  →  No 503s
Code push with bad env var →  Detects ENV violation         →  Rolls back deployment →  Zero downtime
Pod OOMKills repeatedly    →  Identifies root cause (RCA)   →  Restarts + alerts team →  Fast MTTR
```

### The sense → reason → act → verify → learn loop

```
┌─────────────────────────────────────────────────────────────┐
│                         NEXUS                               │
│                                                             │
│  SENSE         REASON          ACT          VERIFY  LEARN  │
│   │               │             │              │       │    │
│  Agents ──► Correlator ──► RCA + Score ──► Runbook ──► KB  │
│  5 types    Cluster events  Gemini/rules   Executor   Adj.  │
│                             ±Confidence   L0–L3      Δconf │
└─────────────────────────────────────────────────────────────┘
```

---

## Architecture

### Planes

| Plane | Phase | What it does |
|-------|-------|-------------|
| **Telemetry** | Ph 1 | OpenTelemetry unified events, Prometheus scrape |
| **Event Bus** | Ph 1 | NATS JetStream — `IncidentEvent` schema |
| **Agent Layer** | Ph 2 | 5 specialist agents (LB, Pod, DB, Repo, Network) |
| **Governance** | Ph 3 | ActionLadder L0–L3, OPA policy, cooldown, circuit breaker, human queue |
| **Reasoning** | Ph 4 | EventCorrelator → RCAEngine (Gemini + rules) → ConfidenceScorer → Orchestrator |
| **Predictive** | Ph 5 | DBTrafficCorrelator, FeaturePipeline, Anomaly Detector (GRU/ZScore), Prescaler |
| **Learning** | Ph 6 | OutcomeStore, KnowledgeBase, RunbookAdvisor, FeedbackLoop |
| **Observability** | Ph 7 | Prometheus metrics, FastAPI status API, Click CLI, Grafana dashboard |

### Package layout

```
src/nexus/
├── bus/              IncidentEvent, NATSClient, SignalType enum
├── telemetry/        OTel collector glue, Prometheus scraper
├── agents/           lb_agent, pod_agent, db_agent, repo_agent, network_agent
├── governance/       action_ladder, runbook (YAML), audit_trail, policy_engine,
│                     cooldown_store, circuit_breaker, human_approval_queue
├── reasoning/        incident_cluster, event_correlator, rca_engine, confidence_scorer,
│                     orchestrator
├── predictive/       feature_pipeline, db_traffic_correlator, anomaly_detector,
│                     traffic_model, prescaler
├── learning/         outcome_store, knowledge_base, runbook_advisor, feedback_loop
└── observability/    metrics (Prometheus), status_api (FastAPI), cli/main.py
```

### Signal flow

```
NGINX / Git / K8s / Prometheus / DB
         │
         ▼ IncidentEvent (NATS)
    5 Domain Agents
         │
         ▼ EventCorrelator (time-window clustering)
    IncidentCluster
         │
         ├──▶ RCAEngine (Gemini 1.5 Flash → JSON | 12-rule fallback)
         │         │
         │    ConfidenceScorer (LLM × agreement × class × history)
         │         │
         ▼         ▼
    ActionLadder → Runbook → RunbookExecutor → AuditTrail
         (OPA + cooldown + CB + human queue)
                                    │
                                    ▼ outcome
                             FeedbackLoop (every 5 min)
                                    │
                             KnowledgeBase (δconfidence per runbook)
                                    │
                             ConfidenceScorer.set_historical_boosts()
```

---

## Quick Start

### Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | |
| Docker + Compose | v2 | For the dev stack |
| `kubectl` | 1.28+ | For autonomous healing |
| Kubernetes cluster | any | local (kind/k3d) or cloud |

### 1 — Start the observability stack

```bash
cd deploy/nexus
docker compose -f docker-compose.dev.yaml up -d
```

Services started:

| Service | URL | Purpose |
|---------|-----|---------|
| **NEXUS API** | http://localhost:8080 | Status API + `/metrics` |
| **Grafana** | http://localhost:3000 | Dashboards (admin / nexus_admin) |
| **Prometheus** | http://localhost:9090 | Metrics storage |
| **NATS** | nats://localhost:4222 | Event bus |
| **NATS Monitor** | http://localhost:8222 | NATS health |
| Loki | — | Log aggregation |
| Tempo | — | Distributed traces |
| Redis | localhost:6379 | Cooldown store |

### 2 — Install NEXUS

```bash
# From the repo root
pip install -e ".[nexus]"
```

### 3 — Verify

```bash
nexus health
# ✓ NEXUS API is up — uptime=12.3s

nexus status
# Shows orchestrator, prescaler, learning plane status

# Open Grafana — NEXUS dashboard is auto-provisioned
open http://localhost:3000
```

### 4 — Configure and run

```bash
# Set environment variables (see Configuration section)
export NEXUS_GEMINI_API_KEY="your-key-here"   # Optional — falls back to rule-based RCA
export NEXUS_AUDIT_DB_PATH="data/nexus_audit.db"

# Start the NEXUS system (example entrypoint — see src/nexus/__main__.py)
python -m nexus
```

---

## CLI Reference

The `nexus` CLI talks to the Status API (`http://localhost:8080` by default).  
Override with `NEXUS_API_URL` or `--url`.

```bash
nexus [--url http://...] COMMAND
```

### System commands

```bash
nexus health                   # Liveness check
nexus status                   # Full system snapshot (orchestrator + prescaler + learning)
nexus last-rca [--n N]         # Last N RCA decisions with confidence and runbook
nexus runbooks [--days D]      # Per-runbook success rates (30-day default)
nexus audit [--n N]            # Tail the AuditTrail
nexus approve ACTION_ID        # Approve a pending human-governance action
nexus approvals                # List all actions awaiting human approval
```

### Prescaler commands

```bash
nexus prescale status          # Stats, mode, recent decisions

nexus prescale set-mode shadow      # Log decisions only (measure precision)
nexus prescale set-mode advisory    # Publish NATS advisory + human approval
nexus prescale set-mode autonomous  # Scale K8s deployments directly (governed)
```

#### Prescaler graduation path

```
SHADOW (default)
    Tracks prediction precision for N=20 decisions
    Graduates when: SMAPE < 25% AND precision ≥ 0.70
         │
         ▼
ADVISORY
    Publishes pre-scale advisory to NATS (nexus approvals pending)
    Operator runs: nexus approve <ID>
         │
         ▼
AUTONOMOUS
    Scales K8s deployment via ActionLadder (L2, governed)
    Full cooldown + circuit breaker apply
```

### Learning commands

```bash
nexus learning status          # FeedbackLoop status + system KPIs
nexus learning run             # Trigger immediate feedback cycle
nexus learning advisor         # RunbookAdvisor recommendations
```

### Example session

```
$ nexus status

╭──────────────────────────────────╮
│         Orchestrator             │
├──────────────────┬───────────────┤
│ events_processed │ 1,247         │
│ clusters_flushed │ 83            │
│ rca_requests     │ 83            │
│ actions_taken    │ 71            │
╰──────────────────┴───────────────╯

╭──────────────────────────────────╮
│           Prescaler              │
├──────────────────┬───────────────┤
│ mode             │ shadow        │
│ decisions_made   │ 12            │
│ precision        │ 0.750         │
│ ready_for_advis. │ False         │
╰──────────────────┴───────────────╯

$ nexus last-rca --n 3

| Cluster ID | Class            | Runbook                          | Level | Confidence |
|------------|------------------|----------------------------------|-------|------------|
| A3F2       | bad_deploy       | runbook_high_error_rate_post_..  | L2    | 78%        |
| B81C       | config_error     | runbook_missing_env_key_v1       | L1    | 91%        |
| 44E7       | resource_exhaust | runbook_pod_crashloop_v1         | L1    | 66%        |

$ nexus learning advisor

| Severity | Runbook                     | Recommendation      |
|----------|-----------------------------|---------------------|
| WARNING  | runbook_pod_crashloop_v1    | ADD_PRE_CHECK       |
| INFO     | runbook_missing_env_key_v1  | PROMOTE_CONFIDENCE  |
```

---

## API Reference

The NEXUS Status API runs at `:8080`. Interactive docs at **http://localhost:8080/docs**.

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/status` | Full system snapshot |
| `GET` | `/metrics` | Prometheus metrics (text format) |

### Reasoning

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/rca/last?n=10` | Last N RCA decisions |

### Governance

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/runbooks/stats?days=30` | Per-runbook statistics |
| `GET` | `/runbooks/list` | Loaded runbook IDs |
| `GET` | `/audit/tail?n=20` | Recent audit records |
| `GET` | `/audit/{incident_id}` | All records for an incident |
| `GET` | `/approvals/pending` | Human approval queue |
| `POST` | `/approve/{action_id}` | Approve a pending action |

### Predictive

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/prescaler` | Prescaler stats + recent decisions |
| `POST` | `/prescaler/mode/{mode}` | Change autonomy mode |

### Learning

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/learning` | FeedbackLoop status |
| `POST` | `/learning/run` | Trigger immediate cycle |
| `GET` | `/knowledge` | KnowledgeBase adjustment table |
| `GET` | `/advisor?days=30` | RunbookAdvisor recommendations |

---

## Configuration

All environment variables have safe defaults. Production deployments should set at minimum `NEXUS_GEMINI_API_KEY` and the DB paths.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_AUDIT_DB_PATH` | `/tmp/nexus_audit.db` | AuditTrail SQLite path |
| `NEXUS_KNOWLEDGE_DB_PATH` | `data/nexus_knowledge.db` | KnowledgeBase SQLite path |
| `NATS_URL` | `nats://localhost:4222` | NATS JetStream URL |
| `NEXUS_LOG_LEVEL` | `INFO` | Logging verbosity |

### Reasoning

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_GEMINI_API_KEY` | _(empty)_ | Gemini 1.5 Flash API key — omit for rule-based RCA |
| `NEXUS_GEMINI_MODEL` | `gemini-1.5-flash` | Gemini model name |

### Predictive

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_SPIKE_MULTIPLIER` | `2.5` | DB query rate-of-change ratio to declare a spike |
| `NEXUS_SPIKE_PREDICTION_HORIZON` | `10` | Minutes ahead to predict |
| `NEXUS_PRESCALE_MODE` | `shadow` | Prescaler mode: `shadow` \| `advisory` \| `autonomous` |
| `NEXUS_PRESCALE_MIN_CONFIDENCE` | `0.55` | Min spike prediction confidence to act |
| `NEXUS_PRESCALE_THRESHOLD_PCT` | `30` | Min % RPS increase to warrant pre-scaling |
| `NEXUS_PRESCALE_MAX_REPLICAS` | `20` | Hard replica cap |
| `NEXUS_PRESCALE_COOLDOWN_S` | `300` | Per-deployment cooldown seconds |
| `NEXUS_GRU_CHECKPOINT_PATH` | _(empty)_ | PyTorch GRU checkpoint — omit for ZScore fallback |

### Governance

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_OPA_URL` | `http://localhost:8181` | Open Policy Agent URL |
| `NEXUS_REDIS_URL` | `redis://localhost:6379` | Redis for cooldown store |
| `NEXUS_CB_THRESHOLD` | `3` | Circuit breaker open after N failures |
| `NEXUS_CB_TIMEOUT_S` | `300` | CB reset timeout seconds |

### Learning

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_FEEDBACK_INTERVAL_S` | `300` | FeedbackLoop poll interval (5 min) |
| `NEXUS_FEEDBACK_WINDOW_DAYS` | `30` | Lookback window for runbook stats |

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_API_URL` | `http://localhost:8080` | Status API URL (used by CLI) |

---

## Grafana Dashboard

The NEXUS dashboard is auto-provisioned at startup. Open **http://localhost:3000** (credentials: `admin` / `nexus_admin`).

### Panels

| Row | Panels |
|-----|--------|
| **Overview** | Autonomous Success Rate · False Heal Rate · Total Actions · Circuit Breaker State |
| **Healing** | Actions by Outcome (timeseries) · Actions by Level (donut) |
| **RCA** | Confidence Score p50/p90 · RCA Duration p50/p99 |
| **Learning** | Runbook Success Rates (bar gauge) · Confidence Adjustments (KB delta) |
| **Prescaler** | Pre-scale Decisions · Prediction Precision · Active Clusters |

### Manual import (if provisioning fails)

1. Open Grafana → **Dashboards → Import**
2. Upload `deploy/nexus/grafana/nexus_dashboard.json`
3. Select the **Prometheus** datasource
4. Click **Import**

---

## Development Guide

### Running the test suite

```bash
# Install dev dependencies
pip install -e ".[nexus,dev]"

# Run tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=src/nexus --cov-report=html
```

### Running individual components

```bash
# Start the dev stack first
docker compose -f deploy/nexus/docker-compose.dev.yaml up -d

# Run a single component for testing
python -m nexus.agents.pod_agent        # Pod agent only
python -m nexus.reasoning.orchestrator  # Orchestrator only

# Status API standalone (auto-reloads on code changes)
uvicorn nexus.observability.status_api:app --port 8080 --reload
```

### Adding a new runbook

Create `src/nexus/governance/runbooks/runbook_<name>_v1.yaml`:

```yaml
id: runbook_<name>_v1
description: "What this runbook fixes"
failure_class: config_error        # or: bad_deploy, resource_exhaustion, dependency_failure
healing_level: 1                   # L0–L3 (higher = more impactful)
blast_radius: single_pod           # single_pod | single_deployment | namespace | cluster
cooldown_seconds: 120
trigger:
  signal_types:
    - env_contract_violation
pre_checks:
  - type: pod_is_unhealthy
    namespace: "{{namespace}}"
    name: "{{pod_name}}"
actions:
  - type: restart_pod
    description: "Restart the affected pod"
    params:
      namespace: "{{namespace}}"
      name: "{{pod_name}}"
post_checks:
  - type: error_rate_below
    threshold: 0.01
    window: 60s
rollback_action:
  type: kubectl_rollout_undo
  params:
    namespace: "{{namespace}}"
    name: "{{deployment}}"
```

### Adding a new agent

1. Create `src/nexus/agents/<name>_agent.py`
2. Subclass or implement the agent interface — subscribe to NATS, emit `IncidentEvent`
3. Set `agent = AgentType.<YOUR_AGENT>` in emitted events
4. Register with `AgentType` enum in `src/nexus/bus/incident_event.py`

### Code quality

```bash
ruff check src/        # Lint
black src/             # Format
mypy src/nexus/        # Type check
```

---

## Phase Roadmap

| Phase | Status | Contents |
|-------|--------|----------|
| **Ph 0** | ✅ | PPA operator (LSTM-based Kubernetes autoscaler) |
| **Ph 1** | ✅ | Telemetry (OTel), Event Bus (NATS), AuditTrail, Runbook schema |
| **Ph 2** | ✅ | 5 domain agents: LB, Pod, DB, Repo, Network |
| **Ph 3** | ✅ | Governance: ActionLadder L0–L3, OPA, cooldown, circuit breaker, human queue |
| **Ph 4** | ✅ | Reasoning: EventCorrelator, RCAEngine (Gemini + rules), ConfidenceScorer, Orchestrator |
| **Ph 5** | ✅ | Predictive: FeaturePipeline, DBTrafficCorrelator, AnomalyDetector, Traffic Model, Prescaler |
| **Ph 6** | ✅ | Learning: OutcomeStore, KnowledgeBase, RunbookAdvisor, FeedbackLoop |
| **Ph 7** | ✅ | Observability: Prometheus metrics, FastAPI, Click CLI, Grafana dashboard |
| **Ph 8** | 🔜 | PPA bug fixes: NaN propagation (§1.4), prediction bounds (§6.2), feature clamping (§3.1) |

### KPIs to track system health

| Metric | Target | How to check |
|--------|--------|--------------|
| MTTD (detection) | < 2 min | Grafana → RCA duration |
| MTTR (recovery) | < 5 min | Audit trail timestamps |
| Autonomous success rate | ≥ 85% | `nexus learning status` |
| False-heal rate | < 15% | Grafana → False Heal Rate panel |
| Prescaler precision | ≥ 70% | `nexus prescale status` |
| Incident recurrence | < 10% | `nexus learning advisor` → CHRONIC_TARGET |

---

## License

NEXUS is open-source software built on the Predictive Pod Autoscaler, licensed under the [MIT License](./LICENSE).
