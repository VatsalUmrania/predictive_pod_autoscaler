# NEXUS + PPA v2.0 — Combined System PRD
## Unified Architecture, Roles & Integration Boundaries

| Field | Value |
|---|---|
| **Document Owner** | PPA / NEXUS Engineering Team |
| **Version** | 1.0 — Unified Architecture |
| **Status** | Active |
| **Date** | May 2026 |
| **Covers** | NEXUS Self-Healing System + PPA v2.0 Adaptive Compute Pressure |

---

> **Executive Summary**
>
> NEXUS is a self-healing Kubernetes infrastructure system that senses failures across six domains, reasons about root cause using LLM-augmented analysis, and acts through a governed healing ladder. PPA v2.0 is the ML prediction engine embedded inside NEXUS that infers the true computational cost of live traffic, detects workload composition shifts, and forecasts scaling pressure 3–10 minutes ahead.
>
> Neither system works optimally in isolation. NEXUS without PPA is reactive — it heals but cannot predict. PPA without NEXUS is ungoverned — it predicts but acts without audit, rollback, or cross-domain awareness. Together they form a complete autonomous SRE system: **NEXUS senses broadly, PPA predicts precisely, NEXUS acts safely.**

---

## Table of Contents

1. [System Philosophy & Design Principles](#1-system-philosophy--design-principles)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Roles & Boundaries — The Definitive Split](#3-roles--boundaries--the-definitive-split)
4. [NEXUS Architecture](#4-nexus-architecture)
   - [4.1 Telemetry Plane](#41-telemetry-plane)
   - [4.2 Event Bus](#42-event-bus)
   - [4.3 Domain Agents](#43-domain-agents)
   - [4.4 Reasoning Plane](#44-reasoning-plane)
   - [4.5 Governance Plane](#45-governance-plane)
   - [4.6 Learning Plane](#46-learning-plane)
   - [4.7 Observability Plane](#47-observability-plane)
5. [PPA v2.0 Architecture](#5-ppa-v20-architecture)
   - [5.1 Telemetry Collector](#51-telemetry-collector)
   - [5.2 Adaptive Cost Learner](#52-adaptive-cost-learner)
   - [5.3 ACP Generator](#53-acp-generator)
   - [5.4 Traffic Composition Shift Detector](#54-traffic-composition-shift-detector)
   - [5.5 Prediction Engine](#55-prediction-engine)
   - [5.6 Confidence-Aware Decision Engine](#56-confidence-aware-decision-engine)
   - [5.7 Cold Start Handler](#57-cold-start-handler)
   - [5.8 Oscillation Damper](#58-oscillation-damper)
6. [Integration Architecture — The Five Bridges](#6-integration-architecture--the-five-bridges)
   - [Bridge 1: DB Signals → PPA Feature Vector](#bridge-1-db-signals--ppa-feature-vector)
   - [Bridge 2: shift_detected → NATS IncidentEvent](#bridge-2-shift_detected--nats-incidentevent)
   - [Bridge 3: ACP + Confidence → Orchestrator](#bridge-3-acp--confidence--orchestrator)
   - [Bridge 4: Runbook Approval → PPA Execute](#bridge-4-runbook-approval--ppa-execute)
   - [Bridge 5: Scaling Outcomes → KnowledgeBase](#bridge-5-scaling-outcomes--knowledgebase)
7. [Data Models](#7-data-models)
   - [7.1 IncidentEvent Schema](#71-incidentevent-schema)
   - [7.2 PPA Feature Vector (17 features)](#72-ppa-feature-vector-17-features)
   - [7.3 ACP Prediction Output](#73-acp-prediction-output)
   - [7.4 Runbook Schema](#74-runbook-schema)
   - [7.5 Audit Trail Record](#75-audit-trail-record)
8. [The Six Failure-Class Playbooks](#8-the-six-failure-class-playbooks)
9. [Healing Action Ladder](#9-healing-action-ladder)
10. [Module Inventory](#10-module-inventory)
    - [10.1 NEXUS Modules](#101-nexus-modules)
    - [10.2 PPA v2.0 Modules](#102-ppa-v20-modules)
11. [Evaluation Criteria](#11-evaluation-criteria)
12. [Implementation Roadmap](#12-implementation-roadmap)
13. [Technology Stack](#13-technology-stack)
14. [What This Is NOT](#14-what-this-is-not)

---

## 1. System Philosophy & Design Principles

### 1.1 The Core Loop

Every healing action in the combined system follows this exact cycle:

```
SENSE → REASON → ACT → VERIFY → LEARN
  │         │       │       │       │
Agents   Orchestrator  Governance  SLO    KB
collect  + PPA RCA    Plane gates  checks update
signals  correlates   action,      post-  runbook
         + picks      or blocks    action quality
         runbook      it           result
```

### 1.2 Design Principles

| Principle | What it means |
|---|---|
| **Sense broadly, predict precisely** | NEXUS agents monitor six domains. PPA provides workload-semantic prediction from that signal. |
| **Every action is governed** | No system — including PPA — patches Kubernetes directly. All changes route through ActionLadder. |
| **Predict before reacting** | PPA forecasts 3–10 minutes ahead. NEXUS should act on predictions, not just incidents. |
| **Fail safely** | Every action has a pre-check, post-check, and automatic rollback. Bad healing is worse than no healing. |
| **Learn continuously** | Every outcome updates the KnowledgeBase. The system should be measurably better after 30 days than day one. |
| **Separation of concerns** | PPA owns prediction. NEXUS owns governance. Neither invades the other's domain. |

### 1.3 What Makes This Novel

| Research Gap | System Answer |
|---|---|
| No end-to-end cross-layer system | NEXUS wires git → config → infra → network → DB into one healing loop |
| DB pattern → traffic prediction unexplored | DBAgent correlates DB access patterns to page traffic 5–10 min ahead |
| LB as passive metric source, never active healer | NginxAgent does predictive traffic shaping pre-emptively |
| Missing `.env` / secrets detection pre-deploy | GitAgent + ConfigAgent do AST scan of code vs secrets at deploy time |
| Safety and rollback guarantees immature | Governance Plane with 4-level healing ladder + atomic rollback per action |
| Continual learning absent | Learning Plane updates runbook quality and incident graph post-incident |
| Volume-only autoscaling | PPA models computational intent, not just request count |
| Static endpoint cost assumptions | PPA learns per-endpoint cost dynamically via EWMA |

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        NEXUS + PPA v2.0                              │
│                                                                      │
│  ① TELEMETRY PLANE                                                   │
│  OTel · Prometheus · Loki · Tempo · K8s Events · NGINX · Git · DB   │
│                          │                                           │
│  ② EVENT BUS (NATS JetStream)                                        │
│  Normalized IncidentEvent schema — all agents publish here           │
│                          │                                           │
│  ③ DOMAIN AGENTS (sense layer)                                       │
│  NginxAgent · GitAgent · MetricsAgent · DBAgent · NetworkAgent       │
│  ConfigAgent · K8sAgent                                              │
│                          │                                           │
│  ④ PPA v2.0 (ML prediction engine — embedded in K8sAgent)           │
│  Telemetry → Cost Learner → ACP Generator → Shift Detector          │
│  → Prediction Engine → Decision Engine                               │
│                          │                                           │
│  ⑤ REASONING PLANE                                                   │
│  EventCorrelator → RCAEngine (Gemini) → ConfidenceScorer            │
│  → Orchestrator (picks runbook + level)                              │
│                          │                                           │
│  ⑥ GOVERNANCE PLANE (safety layer)                                   │
│  ActionLadder L0–L3 · OPA Policy · Cooldown · Circuit Breaker       │
│  · Human Approval Queue · Rollback Registry                          │
│                          │                                           │
│  ⑦ LEARNING PLANE                                                    │
│  OutcomeStore → Labeler → RunbookScorer → KnowledgeBase             │
│  → FeedbackLoop → RunbookAdvisor                                     │
│                          │                                           │
│  ⑧ OBSERVABILITY PLANE                                               │
│  Grafana · FastAPI status API · Click CLI · AuditTrail               │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Roles & Boundaries — The Definitive Split

This section is the single source of truth for what each system owns. When in doubt, refer here.

### 3.1 What NEXUS Owns

| Responsibility | Detail |
|---|---|
| **Signal collection** | All telemetry from all domains (NGINX, Git, K8s, DB, Network, Config) |
| **Event normalization** | All signals flow through `IncidentEvent` schema on NATS — no raw metrics to Orchestrator |
| **Cross-domain correlation** | EventCorrelator clusters signals from multiple agents into one `IncidentCluster` |
| **Root cause analysis** | RCAEngine (Gemini + rules) reasons across all agent signals |
| **Runbook selection** | Orchestrator picks the best runbook based on RCA + confidence + KB history |
| **Governance** | Every action — including PPA scaling — must pass OPA, cooldown, blast-radius, and human queue checks |
| **Action execution** | All Kubernetes API calls, NGINX config changes, Git blocks, DNS flushes |
| **Audit trail** | Every action taken must produce an immutable audit record |
| **Rollback** | Every L1–L3 action has a registered rollback. NEXUS executes it if post-check fails |
| **Learning** | Outcome labeling, runbook quality scoring, KnowledgeBase updates |
| **Observability** | Grafana dashboard, CLI, status API |

### 3.2 What PPA Owns

| Responsibility | Detail |
|---|---|
| **Per-endpoint cost inference** | EWMA-learned computational cost of each HTTP route |
| **ACP computation** | Real-time `ACP(t) = Σ cost(i) × rate(i)` aggregated from all endpoints |
| **Traffic composition monitoring** | Read/write ratios, heavy-endpoint ratios, per-30s |
| **Composition shift detection** | Statistical detection of workload mix changes before CPU reacts |
| **Workload pressure prediction** | XGBoost forecast of `ACP t+3m, t+5m, t+10m` |
| **Prediction confidence scoring** | Per-horizon confidence (0.0–1.0) from XGBoost |
| **Cold start cost estimation** | Cluster-based inherited cost for unseen endpoints |
| **Scaling calculation** | `required_pods = ceil(predicted_ACP / capacity_per_pod)` |
| **Oscillation damping** | Internal cooldown and scale-rate limits before submitting to ActionLadder |

### 3.3 What Neither Owns (Shared Contracts)

| Shared Contract | Owner of schema | Consumers |
|---|---|---|
| `IncidentEvent` on NATS | NEXUS bus module | PPA publishes, all agents publish, Orchestrator consumes |
| `runbook_ppa_acp_scale_v1.yaml` | Jointly maintained | PPA triggers, NEXUS governs |
| `OutcomeRecord` | NEXUS OutcomeStore | PPA writes, NEXUS Learning reads |
| PPA confidence factor in `ConfidenceScorer` | NEXUS confidence module | PPA provides value, NEXUS weighs it |

### 3.4 Hard Boundary Rules

```
PPA MUST NOT:
  ✗ Call k8s.patch() directly — all scaling goes through ActionLadder
  ✗ Write to AuditTrail directly — NEXUS writes audit records
  ✗ Consume raw Prometheus metrics for domain signals outside its feature vector
  ✗ Make RCA decisions — it provides ACP evidence, not root cause conclusions

NEXUS MUST NOT:
  ✗ Replicate PPA's endpoint cost learning — DBAgent provides DB signals, not endpoint weights
  ✗ Override PPA's oscillation damper with its own cooldown for the same deployment
  ✗ Use raw CPU% as a primary scaling trigger when PPA is active and healthy
  ✗ Bypass PPA predictions for pre-scaling when PPA is in shadow or advisory mode
```

---

## 4. NEXUS Architecture

### 4.1 Telemetry Plane

Unified telemetry collection using OpenTelemetry Collector, routing all signals to the appropriate backends.

| Source | Signal Type | Destination |
|---|---|---|
| NGINX access/error logs | HTTP metrics per route | Prometheus + Loki |
| Git webhook events | Push, PR merge, deploy trigger | NATS (GitAgent) |
| Kubernetes Events API | Pod health, OOMKill, rollout status | Prometheus + NATS |
| Prometheus scrape | CPU, memory, RPS, latency | Prometheus TSDB |
| DB engines | Query stats, connection pools, slow queries | NATS (DBAgent) |
| Application (prometheus_client) | Per-endpoint RPS, latency, errors | Prometheus |
| Distributed traces | Span-level latency + errors | Tempo |

### 4.2 Event Bus

All inter-component communication flows through NATS JetStream using the `IncidentEvent` schema. No agent communicates directly with another agent or with the Orchestrator.

```
Agent → NATS → EventCorrelator → IncidentCluster → Orchestrator
```

Benefits of this architecture:
- Agents are stateless and independently testable
- The Orchestrator sees a normalized, deduplicated event stream
- New agents can be added without changing existing components
- NATS replay allows incident post-mortems on real event sequences

### 4.3 Domain Agents

Each agent follows the `observe() → normalize() → publish_to_nats()` pattern. Agents never execute healing actions. All actions are routed through the Governance Plane.

#### NginxAgent
Tails NGINX access and error logs via OTel. Computes per-endpoint RPS, error rate, upstream health, and P95 latency per route. Receives pre-scale signals from PPA and pre-adjusts upstream weights before traffic arrives.

#### GitAgent
Listens to Git webhook events. AST-scans every push for `os.getenv()`, `process.env.X`, and `config.get()` calls to build a required-env contract. Compares against Kubernetes Secrets. Blocks deployments with missing or type-mismatched keys. Also detects accidentally committed secrets.

#### MetricsAgent
Scrapes Prometheus for CPU, memory, RPS, latency percentiles, and error rates. Runs a GRU Autoencoder for multivariate anomaly detection. Has a circuit breaker on Prometheus failures — no socket exhaustion on scrape target outage. Raises `FeatureVectorException` on NaN rather than silently propagating.

#### DBAgent
Multi-DB support: PostgreSQL (`pg_stat_activity`, `pg_stat_statements`), MySQL (`performance_schema`), MongoDB (`currentOp`, `serverStatus`). The core innovation is the `DBTrafficCorrelator`: a Transformer model that predicts HTTP RPS per endpoint 5–10 minutes ahead from DB table-level read/write patterns. Publishes DB signals to NATS that PPA consumes as additional feature inputs.

#### NetworkAgent
Resolves all internal and external DNS names in Kubernetes Services and Ingress. Tests inter-service HTTP connectivity with synthetic probes. Monitors for network policy conflicts.

#### ConfigAgent
Monitors Kubernetes resource state against declared Git manifest state. Detects `kubectl` changes that bypassed GitOps. Continuously validates that running pods have all required env vars populated. Reports drift severity score.

#### K8sAgent
Observes pod health, restart counts, OOMKilled events, deployment rollout status, and HPA state. Is the execution arm for all Kubernetes healing actions after Governance Plane approval. PPA is embedded inside K8sAgent as its scaling sub-module.

### 4.4 Reasoning Plane

The Orchestrator runs a reasoning cycle triggered by new `IncidentCluster` events from the EventCorrelator.

**Inputs per cycle:**
- Normalized incident events from NATS (last 5-minute window)
- Relevant subgraph from Temporal Knowledge Graph
- Top-3 matching past incidents from Incident Memory (cosine similarity)
- Current SLO state (error budget burn rate)
- PPA's `predicted_acp_t5m` and `confidence_t5m` (via NATS or direct call)

**Output per cycle:**
```json
{
  "root_cause_hypothesis": "string",
  "confidence_score": 0.0–1.0,
  "recommended_runbook": "runbook_id",
  "healing_level": 0–3,
  "requires_human_approval": true | false,
  "evidence": ["AgentType:signal_type", ...]
}
```

**Confidence scoring formula:**

```
confidence = LLM_score × agreement_factor × failure_class_weight
             × historical_boost × ppa_predictive_confidence
```

Where `ppa_predictive_confidence` is PPA's XGBoost forecast confidence, fed in as a multiplying factor.

**Fallback behavior:** If Gemini API is unavailable, the Orchestrator falls back to 12-rule deterministic RCA. Seed runbooks from Phase 1 still execute correctly without the LLM.

### 4.5 Governance Plane

> **Every action must pass through here. No exceptions. This includes PPA scaling decisions.**

The Governance Plane applies the following checks in order for every proposed action:

```
1. OPA policy check         — is this action type allowed?
2. Blast-radius check       — does this affect > 20% of cluster?
3. Cooldown check           — has this action run on this target recently?
4. Confidence threshold     — is confidence ≥ level-appropriate minimum?
5. Human approval queue     — if L3 or confidence < 0.85, block for approval
6. Circuit breaker check    — if 3 consecutive post-checks failed, freeze autonomy
```

All checks must pass before any action executes. The results of all checks are written to the AuditTrail.

### 4.6 Learning Plane

Runs every 5 minutes. Processes all closed incidents and updates the system's knowledge.

```
OutcomeStore
    ↓
Incident Outcome Labeler  (healed / worsened / neutral)
    ↓
Runbook Quality Scorer    (updates success rate per runbook_id)
    ↓
KnowledgeBase             (adjusts confidence boost per runbook)
    ↓
ConfidenceScorer          (loads new historical_boost values)
    ↓
RunbookAdvisor            (surfaces recommendations: ADD_PRE_CHECK, PROMOTE, RETIRE)
    ↓
ModelRetrainTrigger       (fires if PPA sMAPE > threshold for 7 days)
```

### 4.7 Observability Plane

| Component | Interface | Purpose |
|---|---|---|
| Prometheus metrics | `/metrics` endpoint | All KPIs as time-series |
| Grafana dashboard | `localhost:3000` | 5-plane unified view |
| FastAPI status API | `localhost:8080` | Live system snapshot |
| Click CLI (`nexus`) | Terminal | Operational commands |
| AuditTrail | SQLite + API | Immutable action history |

---

## 5. PPA v2.0 Architecture

PPA v2.0 is the **ML prediction engine** embedded inside NEXUS's K8sAgent. It is not a standalone system. It owns prediction and cost inference. NEXUS owns governance and execution.

### 5.1 Telemetry Collector

Collects per-request signals for every HTTP endpoint via Prometheus instrumentation.

| Signal | Type | Collection Method | Purpose |
|---|---|---|---|
| `endpoint_route` | String label | prometheus_client Counter labels | Cost attribution per route |
| `http_method` | String label | prometheus_client Counter labels | Distinguish GET vs POST cost |
| `request_payload_bytes` | Histogram | Content-Length header | Payload size → cost correlation |
| `response_payload_bytes` | Histogram | Response Content-Length | Response cost proxy |
| `request_latency_ms` | Histogram (p50/p95/p99) | prometheus_client Histogram | Leading cost indicator |
| `cpu_delta_per_window` | Gauge | cAdvisor `rate()` over 15s | Actual CPU cost per window |
| `memory_delta_per_window` | Gauge | `container_memory_working_set_bytes` delta | Memory pressure per window |
| `http_status_code` | Counter label | prometheus_client labels | Error rate and anomaly detection |

**NaN guard:** Every Prometheus query result is sanitized before entering the feature pipeline. Missing values fall back to rolling-window averages, never zero or NaN.

```python
def safe_query(metric_name, fallback_window=12):
    result = prometheus.query(metric_name)
    if result is None or math.isnan(result):
        return rolling_average(metric_name, steps=fallback_window)
    return clamp(result, FEATURE_BOUNDS[metric_name])
```

### 5.2 Adaptive Cost Learner

Continuously learns the true computational cost of each endpoint using EWMA. No offline retraining required — weights update every 30-second reconciliation cycle.

**Core update formula:**

```python
cost(endpoint, t) = alpha(t) × observed_cost(t) + (1 - alpha(t)) × cost(endpoint, t-1)

observed_cost(t) = w_cpu × cpu_delta + w_mem × mem_delta + w_lat × latency_impact
```

**Dynamic alpha — the key differentiator:**

| Condition | Alpha | Behaviour |
|---|---|---|
| Composition shift detected | 0.7 – 0.9 | Fast reaction — weights update aggressively |
| Traffic stable (low variance) | 0.15 – 0.25 | Smooth — ignores transient noise |
| Default / transitional | 0.4 – 0.5 | Balanced — moderate adaptation |

### 5.3 ACP Generator

Aggregates per-endpoint cost weights and live request rates into the single `ACP(t)` scalar.

```python
ACP(t) = sum(cost(endpoint_i) × rps(endpoint_i, t)  for i in active_endpoints)

# Why ACP is superior to raw RPS:
# 100 RPS of GET /products  →  ACP = 100 × 1.0  = 100   (lightweight)
# 100 RPS of POST /orders   →  ACP = 100 × 8.7  = 870   (write-heavy)
# Same traffic volume. Completely different compute pressure.
```

Per-endpoint ACP contributions are also published for Grafana dashboarding.

### 5.4 Traffic Composition Shift Detector

Monitors the ratio of endpoint types in real time and flags structural shifts before they manifest as CPU spikes. This feature has no equivalent in any mainstream open-source autoscaler.

**Metrics tracked every 30 seconds:**
- `read_ratio` = GET requests / total requests
- `write_ratio` = (POST + PUT + DELETE) / total requests
- `heavy_endpoint_ratio` = high-cost-endpoint requests / total
- `acp_composition_delta` = ACP(t) − ACP(t−2min)

**Shift detection:**
```python
shift_detected = (
    abs(write_ratio_now - write_ratio_2min_ago) > threshold
    or abs(heavy_endpoint_ratio_now - heavy_endpoint_ratio_2min_ago) > 0.15
    or acp_composition_delta > (1.5 × rolling_std_ACP)
)

if shift_detected:
    alpha = 0.8                           # EWMA reacts faster
    publish_incident_event(SignalType.WORKLOAD_SHIFT)   # → NATS Bridge 2
```

### 5.5 Prediction Engine

Forecasts ACP 3, 5, and 10 minutes ahead. XGBoost is the primary model. LSTM (TFLite) is an optional upgrade for apps with strong sequential patterns.

**17-feature input vector:**

| # | Feature | Category | Description |
|---|---|---|---|
| 1 | `acp_1m` | ACP Rolling | ACP averaged over last 1 minute |
| 2 | `acp_5m` | ACP Rolling | ACP averaged over last 5 minutes |
| 3 | `acp_15m` | ACP Rolling | ACP averaged over last 15 minutes |
| 4 | `acp_delta_1m` | ACP Momentum | ACP change over last 1 minute |
| 5 | `acp_delta_5m` | ACP Momentum | ACP change over last 5 minutes |
| 6 | `read_ratio` | Composition | Fraction of GET requests |
| 7 | `write_ratio` | Composition | Fraction of write requests |
| 8 | `heavy_endpoint_ratio` | Composition | Fraction from high-cost endpoints |
| 9 | `shift_detected` | Composition | Composition shift flag (bool) |
| 10 | `hour_sin` | Cyclical Time | sin(2π × hour / 24) |
| 11 | `hour_cos` | Cyclical Time | cos(2π × hour / 24) |
| 12 | `dow_sin` | Cyclical Time | sin(2π × day_of_week / 7) |
| 13 | `dow_cos` | Cyclical Time | cos(2π × day_of_week / 7) |
| 14 | `is_weekend` | Cyclical Time | 1 if Saturday or Sunday |
| 15 | `acp_24h_ago` | Seasonality | ACP at this exact time yesterday |
| 16 | `acp_7d_ago` | Seasonality | ACP at same time last week |
| 17 | `replicas_normalized` | Infrastructure | Current replicas / max_replicas |

Features 15–17 are augmented with `db_read_rate_delta` and `db_write_rate_delta` from DBAgent (Bridge 1), giving 19 features in the integrated configuration.

**Prediction output:**
```python
{
    "predicted_acp_t3m":  float,
    "predicted_acp_t5m":  float,
    "predicted_acp_t10m": float,
    "confidence_t3m":     float,   # 0.0 – 1.0
    "confidence_t5m":     float,
    "confidence_t10m":    float,
}
```

All outputs are clamped: `predicted_acp >= 0` and implied replicas within `[minReplicas, maxReplicas]`.

### 5.6 Confidence-Aware Decision Engine

Maps predicted ACP and confidence scores to scaling intents. PPA generates the intent — NEXUS ActionLadder approves and executes it.

| Condition | Intent | Level |
|---|---|---|
| confidence > 0.85, ACP rising | Scale aggressively | L2 |
| confidence 0.65–0.85, ACP rising | Scale conservatively (+1 pod) | L1 |
| `shift_detected`, confidence > 0.70 | Prewarm pods immediately | L1 |
| confidence < 0.65 | Hold — wait next cycle | L0 (log only) |
| `anomaly_detected` | Freeze — alert only | L0 (alert) |
| ACP falling, confidence > 0.75 | Scale down (with cooldown) | L1 |

PPA submits the intent as a NATS event. NEXUS ActionLadder receives it, applies governance checks, and executes if approved.

### 5.7 Cold Start Handler

New endpoints have no cost history. The cold start handler clusters the new endpoint to the nearest known endpoint and inherits its cost profile.

**Clustering criteria:**
```
1. HTTP method        (GET / POST / PUT / DELETE)
2. Payload size bucket (small < 1KB / medium < 10KB / large >= 10KB)
3. Route depth        (number of path segments)
```

After ≥ 50 real requests, real EWMA observations fully replace the inherited weight.

### 5.8 Oscillation Damper

Prevents the feedback loop where scaling changes latency → latency changes ACP → ACP triggers further scaling. This is PPA's internal throttle before it even submits to ActionLadder.

| Rule | Value |
|---|---|
| Scale-up cooldown | 2 minutes |
| Scale-down cooldown | 5 minutes |
| Rapid-scale anomaly threshold | 3 scale events in 10 minutes → anomaly mode |
| Maximum scale step up | 2× current replicas per cycle |
| Maximum scale step down | 50% reduction per cycle |

Note: NEXUS ActionLadder applies its own cooldown on top of this. Both must pass before execution.

---

## 6. Integration Architecture — The Five Bridges

The following five integration points are the only places where NEXUS and PPA exchange data. All other logic is internal to each system.

```
NEXUS Domain Agents  ──Bridge 1──►  PPA Feature Vector
PPA Shift Detector   ──Bridge 2──►  NATS Event Bus
PPA Prediction Eng.  ──Bridge 3──►  NEXUS Orchestrator
NEXUS ActionLadder   ──Bridge 4──►  PPA Decision Engine
PPA Decision Engine  ──Bridge 5──►  NEXUS Learning Plane
```

### Bridge 1: DB Signals → PPA Feature Vector

**Direction:** NEXUS → PPA

**What flows:** DBAgent publishes table-level read/write rate deltas to NATS. PPA's `features.py` subscribes and augments its feature vector with two additional inputs.

**Why:** DB write pressure on the `orders` table predicts `POST /orders` HTTP spikes before they appear in RPS. This gives PPA a 2–5 minute earlier warning signal.

```python
# DBAgent publishes to NATS subject: nexus.signals.db
IncidentEvent(
    agent=AgentType.DB,
    signal_type=SignalType.DB_PATTERN,
    context={
        "db_read_rate_delta":  float,   # change in reads/sec over 2 min
        "db_write_rate_delta": float,   # change in writes/sec over 2 min
        "table": "orders"
    }
)

# PPA features.py subscribes and adds to vector:
feature_vector["db_read_rate_delta"]  = nats_event.context["db_read_rate_delta"]
feature_vector["db_write_rate_delta"] = nats_event.context["db_write_rate_delta"]
```

### Bridge 2: shift_detected → NATS IncidentEvent

**Direction:** PPA → NEXUS

**What flows:** When PPA's shift detector fires, it publishes an `IncidentEvent` to NATS. The Orchestrator receives it as additional evidence. NginxAgent also subscribes and pre-adjusts upstream weights.

**Why:** Composition shift is a cross-domain signal. NginxAgent can preemptively shift traffic away from pods that are about to become saturated. The Orchestrator can raise its confidence in any co-occurring anomaly signals.

```python
# In PPA shift_detector.py
if shift_detected:
    await nats_client.publish(IncidentEvent(
        agent=AgentType.K8S,
        signal_type=SignalType.WORKLOAD_SHIFT,
        severity="warning",
        context={
            "write_ratio_delta":   delta,
            "acp_composition_delta": acp_delta,
            "shift_confidence":    confidence,
            "target_deployment":   target
        }
    ))
```

### Bridge 3: ACP + Confidence → Orchestrator

**Direction:** PPA → NEXUS

**What flows:** PPA's `predicted_acp_t5m` and `confidence_t5m` are published to NATS and consumed by the Orchestrator's ConfidenceScorer as a multiplying factor.

**Why:** The Orchestrator's confidence score becomes richer when combined with PPA's workload-semantic prediction. Low PPA confidence on a spike prediction → Orchestrator holds back on L2 actions. High PPA confidence → Orchestrator can approve aggressive pre-scaling without human approval.

```python
# In NEXUS confidence.py — PPA feeds one factor
nexus_confidence = (
    llm_score
    × agreement_factor
    × failure_class_weight
    × historical_boost
    × ppa_predictive_confidence    # ← PPA's contribution via Bridge 3
)
```

### Bridge 4: Runbook Approval → PPA Execute

**Direction:** NEXUS → PPA

**What flows:** PPA submits a scaling intent as an `IncidentEvent`. NEXUS ActionLadder processes it through governance checks and either approves (K8sAgent executes) or blocks (logs + escalates).

**Why:** PPA must never patch Kubernetes directly. Governance — OPA checks, cooldown, blast-radius, human queue — protects the cluster from PPA misfires. This bridge ensures PPA's predictions are acted on safely.

```yaml
# runbook_ppa_acp_scale_v1.yaml
id: runbook_ppa_acp_scale_v1
description: "Pre-scale deployment based on PPA ACP forecast"
failure_class: traffic_pressure
healing_level: 2
blast_radius: single_deployment
cooldown_seconds: 120
trigger:
  signal_types:
    - acp_spike_predicted
    - composition_shift_detected
pre_checks:
  - assert: "predicted_acp > current_acp * 1.30"
  - assert: "ppa_confidence > 0.65"
  - assert: "current_replicas < max_replicas"
actions:
  - type: scale_deployment
    description: "Scale to PPA-recommended replica count"
    params:
      namespace: "{{namespace}}"
      deployment: "{{target_deployment}}"
      replicas: "{{desired_replicas}}"
post_checks:
  - metric: "http_error_rate"
    condition: "< 0.02"
    window: "60s"
    timeout: "120s"
rollback_if_post_check_fails: true
```

### Bridge 5: Scaling Outcomes → KnowledgeBase

**Direction:** PPA → NEXUS

**What flows:** After every scaling action completes (success or failure), PPA reports a structured outcome record to NEXUS's `OutcomeStore`. The Learning Plane processes it.

**Why:** PPA has no self-improvement mechanism in isolation. NEXUS's Learning Plane uses outcomes to adjust confidence boosts, update runbook success rates, and surface recommendations via `nexus learning advisor`. After 30 days, PPA's thresholds and timing should be objectively better.

```python
# In PPA outcome_reporter.py (new module)
await outcome_store.record(OutcomeRecord(
    incident_id=cluster_id,
    runbook_id="runbook_ppa_acp_scale_v1",
    predicted_acp=predicted_acp_t5m,
    actual_acp=measured_acp_5min_later,      # measured post-scale
    replicas_before=current_replicas,
    replicas_after=desired_replicas,
    p99_latency_before=p99_before,
    p99_latency_after=p99_after,
    error_rate_after=error_rate,
    success=error_rate < 0.01,
    timestamp=datetime.utcnow()
))
```

---

## 7. Data Models

### 7.1 IncidentEvent Schema

All inter-component communication uses this schema.

```python
class IncidentEvent(BaseModel):
    event_id:       UUID
    timestamp:      datetime
    agent:          AgentType       # LB | POD | DB | REPO | NETWORK | K8S | CONFIG
    signal_type:    SignalType       # WORKLOAD_SHIFT | DB_PATTERN | ERROR_SPIKE | ...
    severity:       Literal["info", "warning", "critical"]
    namespace:      str
    context:        dict            # agent-specific payload
    raw:            dict | None     # original telemetry for debugging

class SignalType(str, Enum):
    WORKLOAD_SHIFT         = "workload_shift"
    ACP_SPIKE_PREDICTED    = "acp_spike_predicted"
    DB_PATTERN             = "db_pattern"
    ERROR_SPIKE            = "error_spike"
    CONFIG_DRIFT           = "config_drift"
    MISSING_ENV_KEY        = "missing_env_key"
    POD_CRASHLOOP          = "pod_crashloop"
    DNS_FAILURE            = "dns_failure"
    ANOMALY_SIGNAL         = "anomaly_signal"
    COMPOSITION_SHIFT      = "composition_shift_detected"
```

### 7.2 PPA Feature Vector (17–19 features)

Each row in the training dataset and each inference input vector.

| # | Feature | Type | Notes |
|---|---|---|---|
| 1–3 | `acp_1m`, `acp_5m`, `acp_15m` | float | ACP rolling averages |
| 4–5 | `acp_delta_1m`, `acp_delta_5m` | float | ACP rate of change |
| 6–8 | `read_ratio`, `write_ratio`, `heavy_endpoint_ratio` | float | Traffic composition |
| 9 | `shift_detected` | bool | Composition shift flag |
| 10–14 | `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos`, `is_weekend` | float | Cyclical time encoding |
| 15–16 | `acp_24h_ago`, `acp_7d_ago` | float | Seasonality signals |
| 17 | `replicas_normalized` | float | Infrastructure state |
| 18* | `db_read_rate_delta` | float | From DBAgent via Bridge 1 |
| 19* | `db_write_rate_delta` | float | From DBAgent via Bridge 1 |

\* Features 18–19 are added in the integrated NEXUS+PPA configuration only.

**Prediction targets:**

| Target | Description |
|---|---|
| `acp_t3m` | ACP value 3 minutes ahead |
| `acp_t5m` | ACP value 5 minutes ahead — primary horizon |
| `acp_t10m` | ACP value 10 minutes ahead — long-horizon |
| `replicas_t3m/t5m/t10m` | `ceil(acp_tXm / capacity_per_pod)` |

### 7.3 ACP Prediction Output

```python
@dataclass
class ACPPrediction:
    predicted_acp_t3m:   float
    predicted_acp_t5m:   float
    predicted_acp_t10m:  float
    confidence_t3m:      float    # 0.0 – 1.0
    confidence_t5m:      float
    confidence_t10m:     float
    desired_replicas:    int      # ceil(predicted_acp_t5m / capacity_per_pod)
    action:              str      # scale_up | scale_down | hold | prewarm | freeze
    shift_detected:      bool
    timestamp:           datetime
```

### 7.4 Runbook Schema

```yaml
id:                  string          # unique runbook identifier
description:         string
failure_class:       string          # bad_deploy | config_error | traffic_pressure | ...
healing_level:       0 | 1 | 2 | 3
blast_radius:        string          # single_pod | single_deployment | namespace | cluster
cooldown_seconds:    int
trigger:
  signal_types:      list[SignalType]
pre_checks:          list[assert]
actions:             list[action]
post_checks:         list[metric_check]
rollback_if_post_check_fails: bool
rollback_action:     action | null
```

### 7.5 Audit Trail Record

Every action writes exactly one record. No silent actions.

```json
{
  "action_id":           "uuid",
  "timestamp":           "ISO8601",
  "triggered_by":        "OrchestratorAgent | human:username",
  "runbook_id":          "runbook_ppa_acp_scale_v1",
  "healing_level":       2,
  "target":              "default/shop-api",
  "pre_check_results":   { "passed": true, "details": {} },
  "execution_outcome":   "success | failed | rolled_back",
  "post_check_results":  { "slo_restored": true, "p99_after": 145 },
  "rollback_triggered":  false,
  "incident_id":         "uuid",
  "ppa_prediction":      { "acp_t5m": 342.1, "confidence": 0.88 }
}
```

---

## 8. The Six Failure-Class Playbooks

Each playbook shows how NEXUS agents and PPA collaborate to detect, reason, and heal.

### Playbook 1 — Bad Code Push
```
GitAgent: deploy event detected (SHA abc123)
MetricsAgent: error rate > 5% within 2 min of deploy
PPA: ACP spike detected (composition unchanged — error load, not traffic)
Orchestrator: correlate deploy SHA + error spike → confidence 0.91
Governance: L3 action (deploy rollback)
K8sAgent: kubectl rollout undo
Post-check: error rate < 2% within 60s → PASS
KB: "deploy abc123 introduced regression — rollback confirmed fix"
```

### Playbook 2 — Missing `.env` Key
```
GitAgent: AST scan → STRIPE_WEBHOOK_SECRET required in new code
ConfigAgent: key absent from production Secret
Governance: L0 action — block CI/CD pipeline, open GitHub issue
Developer: adds key to vault
ConfigAgent: key present → deploy proceeds
Post-check: no config drift events for 10 min → PASS
```

### Playbook 3 — Pod OOMKilled
```
K8sAgent: OOMKilled event on payments-api
MetricsAgent: memory utilization > 95% for 3 min
Orchestrator: OOMKill + memory pressure = hitting container limit
Governance: L1 → restart pod + emit VPA hint (memory +25%)
K8sAgent: apply VPA hint on next rollout
Post-check: pod Running, memory < 80% → PASS
KB: "payments-api needs 512Mi → 768Mi on high-traffic days"
```

### Playbook 4 — DNS / Routing Failure
```
NetworkAgent: DNS resolution fails for payments-svc from cluster
Orchestrator: correlates with CoreDNS ConfigMap drift (ConfigAgent alert)
Governance: L1 → flush CoreDNS cache
Post-check: DNS resolves in < 200ms → PASS
If not: L2 → revert ConfigMap
```

### Playbook 5 — DB Saturation
```
DBAgent: pg_stat_activity shows 95% connections used
DBAgent: one query averaging 8s (missing index on orders.user_id)
Orchestrator: connection pool exhaustion + slow query → L2
Governance: throttle /reports endpoint + shift reads to replica
Post-check: connections < 70%, P95 < 200ms → PASS
Recommendation: add index on orders.user_id
```

### Playbook 6 — DB-Predicted Traffic Spike (PPA-led)
```
DBAgent: SELECT on products table ↑ 300%
Bridge 1: db_read_rate_delta → PPA feature vector
PPA Prediction Engine: predicts /shop ACP × 4 in 8 min (confidence 0.88)
Bridge 3: predicted_acp_t5m → Orchestrator
Bridge 2: shift_detected IncidentEvent → NATS
Orchestrator: advisory pre-scale signal
Governance: L1 → K8sAgent pre-scales shop-api +3 replicas
NginxAgent: receives Bridge 2 event → pre-adjusts upstream weights
Traffic arrives → zero dropped requests
Post-check: P95 < 200ms → PASS
Bridge 5: outcome reported → KB updated
```

---

## 9. Healing Action Ladder

| Level | Actions | Governance Gate | Auto-Rollback | PPA Relevance |
|---|---|---|---|---|
| **L0** | Detect + alert, log to AuditTrail | None | N/A | PPA anomaly freeze emits L0 alert |
| **L1** | Restart pod, flush DNS, scale ±1 step, prewarm pods | OPA check | Yes, automatic | PPA prewarm and conservative scale-up |
| **L2** | Canary shift, drain node, rate-limit endpoint, scale ±3 | OPA + cooldown | Yes, automatic | PPA aggressive scale-up (confidence > 0.85) |
| **L3** | Rollback deploy, patch config/secret, scale ±10 | OPA + human approval if confidence < 0.85 | Yes + incident report | PPA large-scale pre-provisioning (rare) |

### Governance Rules (OPA)

```
Action allowlist:     Only pre-approved action types can execute
Blast radius:         No action affects > 20% of cluster at once
Cooldown:             Same action on same target cannot repeat within N seconds
Confidence floor:     L3 requires confidence ≥ 0.85 or human approval
Circuit breaker:      3 consecutive post-check failures → freeze autonomy, escalate
```

---

## 10. Module Inventory

### 10.1 NEXUS Modules

| Module | Path | Responsibility |
|---|---|---|
| `nats_client.py` | `bus/` | Publish/subscribe abstraction over NATS JetStream |
| `incident_event.py` | `bus/` | Pydantic IncidentEvent schema + SignalType enum |
| `otel_collector_config.yaml` | `telemetry/` | OpenTelemetry routing config |
| `log_shipper.py` | `telemetry/` | NGINX log → OTel → Loki pipeline |
| `nginx_agent.py` | `agents/` | NGINX log tailing + upstream weight adjustment |
| `git_agent.py` | `agents/` | Git webhook + AST env contract validator |
| `metrics_agent.py` | `agents/` | Prometheus scrape + GRU anomaly detection |
| `db_agent.py` | `agents/` | Multi-DB query stats + DBTrafficCorrelator |
| `network_agent.py` | `agents/` | DNS probes + network policy checks |
| `config_agent.py` | `agents/` | IaC drift detection + runtime env validation |
| `k8s_agent.py` | `agents/` | Pod health + execution arm for K8s actions |
| `policy_engine.py` | `governance/` | OPA HTTP API client |
| `runbook.py` | `governance/` | YAML runbook loader + pre/post check executor |
| `action_ladder.py` | `governance/` | L0/L1/L2/L3 router |
| `rollback_registry.py` | `governance/` | Atomic rollback wrapper per action type |
| `cooldown_store.py` | `governance/` | Redis-backed action cooldown tracker |
| `audit_trail.py` | `governance/` | Immutable append-only audit log |
| `orchestrator.py` | `reasoning/` | Gemini API reasoning loop |
| `confidence.py` | `reasoning/` | Multi-factor confidence scorer |
| `rca_prompt.py` | `reasoning/` | Structured RCA prompt templates |
| `incident_graph.py` | `kb/` | Temporal Knowledge Graph (NetworkX → Neo4j) |
| `incident_store.py` | `kb/` | PostgreSQL incident history |
| `runbook_matcher.py` | `kb/` | Cosine similarity past-incident retriever |
| `outcome_labeler.py` | `learning/` | healed / worsened / neutral classifier |
| `runbook_scorer.py` | `learning/` | Runbook success rate updater |
| `retrain_trigger.py` | `learning/` | CI retraining trigger on drift > threshold |
| `metrics.py` | `observability/` | Prometheus metrics exposition |
| `status_api.py` | `observability/` | FastAPI status + governance endpoints |
| `cli/main.py` | `observability/` | Click CLI (`nexus` command) |

### 10.2 PPA v2.0 Modules

| Module | Status | Responsibility |
|---|---|---|
| `config.py` | Existing | Cluster-wide defaults — PROMETHEUS_URL, TIMER_INTERVAL |
| `telemetry.py` | **New** | Per-endpoint signal collection |
| `cost_learner.py` | **New** | EWMA per-endpoint cost weights + dynamic alpha |
| `acp.py` | **New** | ACP(t) computation + composition ratio calculation |
| `shift_detector.py` | **New** | Traffic composition shift detection → NATS Bridge 2 |
| `cold_start.py` | **New** | Endpoint clustering + inherited cost weights |
| `nan_guard.py` | **New** | Sanitize all Prometheus inputs before feature build |
| `outcome_reporter.py` | **New** | Report scaling outcomes to NEXUS OutcomeStore Bridge 5 |
| `features.py` | Updated | 17→19 feature ACP vector, consumes DBAgent signals Bridge 1 |
| `predictor.py` | Updated | XGBoost primary, output clamped, LSTM optional |
| `scaler.py` | Updated | Submits to NATS instead of direct k8s.patch() |
| `main.py` | Updated | Per-CR state registry, shadow mode, anomaly freeze |

**PPA Bug Fixes (Phase 8):**

| Bug | Fix |
|---|---|
| NaN propagation (§1.4) | `nan_guard.py` sanitizes all Prometheus inputs with rolling-average fallback |
| Prediction bounds violation (§6.2) | All outputs clamped to `[0, maxReplicas]` before emission |
| Feature clamping missing (§3.1) | `FEATURE_BOUNDS` dict enforced in `safe_query()` before XGBoost input |
| Training-serving skew (§1.1) | `referenceReplicas` normalization aligned between train and serve |
| History wipe on model upgrade (§1.3) | Rolling window preserved in stateful `CRState` across model reloads |

---

## 11. Evaluation Criteria

### NEXUS Reliability KPIs

| KPI | Target | Measurement |
|---|---|---|
| MTTD (detection) | < 90 seconds | Time from fault injection to first NATS IncidentEvent |
| MTTR L1/L2 | < 5 minutes | Time from first event to post-check SLO restored |
| MTTR L3 | < 10 minutes | Same, for high-risk actions |
| Time-to-safe after bad deploy | < 4 minutes | Deploy event to error rate < 2% |
| Incident recurrence rate | < 10% | Incidents reopening within 48h for same root cause |

### NEXUS Autonomy KPIs

| KPI | Target | Measurement |
|---|---|---|
| L1 autonomous success rate | > 90% | Post-check SLO pass without human intervention |
| L2 autonomous success rate | > 80% | Post-check SLO pass |
| False-heal rate | < 5% | Actions that trigger post-check failure |
| False-positive alert rate | < 10% | Incident events with no confirmed fault |
| Rollback success rate | > 95% | Rollbacks that restore SLO within timeout |

### PPA Prediction KPIs

| KPI | Target | Measurement |
|---|---|---|
| ACP prediction MAE (t+5m) | < 15% of mean ACP | Cross-validation on held-out data |
| Composition shift detection recall | > 90% | Labelled spike events replay |
| p99 latency during spike | < 200ms | vs HPA baseline (often > 500ms) |
| Scaling reaction time | < 90s from spike | Time from ACP delta to pod Ready |
| Dropped requests during spike | < 0.1% | vs HPA baseline (up to 2–5%) |
| Scaling oscillations per hour | < 3 | Count of scale events per 60 min |
| Pod utilization average | > 70% | vs HPA (often 40–55%) |
| Cold start cost accuracy | > 80% | vs observed cost after 50 requests |

### Integration KPIs

| KPI | Target | Measurement |
|---|---|---|
| DBAgent → PPA prediction improvement | > 5% MAE reduction | A/B: with vs without db_rate_delta features |
| Shadow mode precision (pre-scaling) | > 80% | True spikes caught / total predictions |
| Pre-deploy block rate (missing env) | ≥ 80% | Simulated missing-env incidents caught |
| Governance gate latency | < 500ms | Median OPA check time per action |

---

## 12. Implementation Roadmap

| Phase | Weeks | Deliverable | NEXUS / PPA / Both |
|---|---|---|---|
| 1 | 1–3 | Telemetry plane + NATS event bus + 5 seed runbooks | NEXUS |
| 2 | 4–6 | All 7 domain agents (including K8sAgent wrapping PPA v1) | NEXUS |
| 3 | 7–8 | Governance plane: ActionLadder, OPA, cooldown, rollback, audit trail | NEXUS |
| 4 | 9–11 | Reasoning plane: Orchestrator, Gemini RCA, ConfidenceScorer, KnowledgeGraph | NEXUS |
| 5 | 12–13 | Predictive layer: PPA v2.0 ACP engine replaces existing Prescaler | **Both** |
| 6 | 14–15 | Learning plane: OutcomeStore, KnowledgeBase, FeedbackLoop | NEXUS |
| 7 | 16 | Dashboard + CLI extensions; all 5 bridges wired and tested | **Both** |
| 8 | 16 | PPA bug fixes: NaN, bounds, clamping, training-serving skew | PPA |

### Phase 5 Sprint Detail (PPA Integration)

| Sprint | Deliverable | Owner |
|---|---|---|
| 5.1 | `telemetry.py` + `acp.py` — ACP(t) visible in Grafana | PPA |
| 5.2 | `cost_learner.py` + `shift_detector.py` + Bridge 2 | PPA |
| 5.3 | `features.py` updated to 19-feature vector + Bridge 1 | Both |
| 5.4 | XGBoost `predictor.py` trained on ACP dataset | PPA |
| 5.5 | `scaler.py` → emits to NATS + Bridge 3 wired | Both |
| 5.6 | `runbook_ppa_acp_scale_v1.yaml` + Bridge 4 governance | Both |
| 5.7 | `cold_start.py` + `nan_guard.py` | PPA |
| 5.8 | `outcome_reporter.py` + Bridge 5 wired | Both |

---

## 13. Technology Stack

| Component | Technology | Owned By |
|---|---|---|
| Language | Python 3.11 | Both |
| Agent framework | asyncio + Pydantic | NEXUS |
| Event bus | NATS JetStream | NEXUS |
| LLM reasoning | Gemini 1.5 Flash → Ollama (future) | NEXUS |
| Policy engine | OPA (Open Policy Agent) | NEXUS |
| K8s operator framework | Kopf | PPA / K8sAgent |
| Canary deployments | Argo Rollouts | NEXUS |
| Primary ML model | XGBoost >= 1.7 | PPA |
| Optional ML model | Keras LSTM + TFLite | PPA |
| Anomaly detection | GRU Autoencoder (PyTorch) | NEXUS MetricsAgent |
| DB→traffic correlation | Transformer encoder-decoder (PyTorch) | NEXUS DBAgent |
| Cost learning | EWMA (custom Python) | PPA |
| Knowledge graph | NetworkX → Neo4j (prod) | NEXUS |
| Incident store | PostgreSQL | NEXUS |
| Cooldown / cache | Redis | NEXUS |
| Metrics | Prometheus + kube-prometheus-stack | Both |
| Telemetry | OpenTelemetry Collector | NEXUS |
| App instrumentation | prometheus_client | PPA |
| Visualization | Grafana | Both |
| Load testing | Locust (ChaoticLoadShape) | Both |
| Local Kubernetes | Minikube (KVM2) | Both |
| DB adapters | psycopg3, PyMySQL, pymongo | NEXUS |
| ❌ NOT used | Prophet | — |
| ❌ NOT used (yet) | Reinforcement Learning | — |

---

## 14. What This Is NOT

| Out of Scope | Why Excluded | Future Version |
|---|---|---|
| Reinforcement Learning scaling policy | Hard to stabilize, debug, and explain at this stage | v3.0 after ACP baseline proven |
| Full causal inference for attribution | Requires distributed tracing infrastructure. EWMA approximation is sufficient. | v3.0 with OpenTelemetry traces |
| Cross-application transfer learning | Scope explosion. Meta-learning is multiple thesis domains. | Research extension |
| Multi-cluster / federated deployment | Operational complexity far exceeds project scope | Production version |
| GPU utilization tracking | Test-app is CPU-bound. Different telemetry stack needed. | v2.1 for ML-serving workloads |
| Prophet-based forecasting | Designed for daily/weekly seasonality. Wrong update cadence for 3–10 min horizons. | Not planned |
| NEXUS agents replacing PPA prediction | Agents sense and normalize. PPA predicts. These roles must not merge. | Never — this is a design principle |
| PPA calling k8s.patch() directly | All scaling must route through ActionLadder. No exceptions. | Never — this is a hard boundary |

---

> **The One-Line Summary**
>
> NEXUS is the nervous system — it senses, reasons, governs, and learns. PPA is the predictive brain — it infers workload semantics and forecasts compute pressure before it arrives. Together they form the first Kubernetes-native system that acts on computational intent rather than infrastructure metrics.
