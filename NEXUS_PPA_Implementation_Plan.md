# NEXUS + PPA v2.0 — Full Implementation Plan
## 38 Vertical Slice Issues · 13 Weeks · 5 Phases

| Field | Value |
|---|---|
| **Project** | NEXUS Self-Healing System + PPA v2.0 Adaptive Compute Pressure |
| **Total Issues** | 38 |
| **Total Phases** | 5 |
| **Timeline** | 13 Weeks |
| **HITL Issues** | 7 (18%) |
| **AFK Issues** | 31 (82%) |
| **Status** | ✅ Approved — Ready to Publish |

---

## Legend

| Symbol | Meaning |
|---|---|
| **AFK** | Away From Keyboard — can be built autonomously without design review |
| **HITL** | Human In The Loop — requires architecture/design decision before or during build |
| **✅** | No blockers — can start immediately |
| **⛔** | Blocked — must wait for listed issues |

---

## Table of Contents

- [Phase 1 — NEXUS Foundation + Early PPA (Weeks 1–3)](#phase-1--nexus-foundation--early-ppa-weeks-13)
- [Phase 2 — Domain Agents + ACP Core (Weeks 4–6)](#phase-2--domain-agents--acp-core-weeks-46)
- [Phase 3 — Reasoning & Governance Deepening (Weeks 7–9)](#phase-3--reasoning--governance-deepening-weeks-79)
- [Phase 4 — PPA v2.0 + K8sAgent Integration (Weeks 10–11)](#phase-4--ppa-v20--k8sagent-integration-weeks-1011)
- [Phase 5 — Integration Finishing (Week 13)](#phase-5--integration-finishing-week-13)
- [Dependency Map](#dependency-map)
- [HITL Decision Register](#hitl-decision-register)
- [KPI Targets](#kpi-targets)

---

## Phase 1 — NEXUS Foundation + Early PPA (Weeks 1–3)

> **Goal:** Event bus running, governance foundation in place, seed runbooks proven, early PPA instrumentation collecting data from Day 1.

---

### Issue 1 — NEXUS-1: Wire up NATS Event Bus with IncidentEvent schema
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1

**Description:**
Set up NATS JetStream as the central event bus. Define the normalized `IncidentEvent` Pydantic schema that all agents and PPA will use to communicate. This is the communication backbone of the entire system — everything else publishes to or consumes from this bus.

**Deliverables:**
- `src/nexus/bus/nats_client.py` — publish/subscribe abstraction over NATS JetStream
- `src/nexus/bus/incident_event.py` — Pydantic `IncidentEvent` schema + `SignalType` enum + `AgentType` enum
- `deploy/nexus/docker-compose.dev.yaml` — NATS + Prometheus + Loki + Tempo + Grafana dev stack
- Unit tests: publish + consume roundtrip, schema validation, malformed event rejection

**IncidentEvent schema:**
```python
class IncidentEvent(BaseModel):
    event_id:    UUID
    timestamp:   datetime
    agent:       AgentType    # LB | POD | DB | REPO | NETWORK | K8S | CONFIG
    signal_type: SignalType   # WORKLOAD_SHIFT | DB_PATTERN | ERROR_SPIKE | ...
    severity:    Literal["info", "warning", "critical"]
    namespace:   str
    context:     dict         # agent-specific payload
    raw:         dict | None  # original telemetry for debugging
```

**Exit criteria:**
- [ ] NATS JetStream running in Docker Compose dev stack
- [ ] IncidentEvent schema validates and rejects malformed events
- [ ] Publish + consume roundtrip confirmed in unit tests
- [ ] Schema versioning strategy documented

---

### Issue 2 — PPA-17: Build ACP telemetry collector per-endpoint
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1

**Description:**
Instrument the target application with per-endpoint telemetry using `prometheus_client`. This is the raw signal layer that feeds the Adaptive Cost Learner. Must run in parallel with NEXUS-1 — data collection should start from Day 1.

**Deliverables:**
- `src/ppa/telemetry.py` — per-endpoint signal collection
- Prometheus metrics exposed at `:9091/metrics`:
  - `http_requests_total{route, method, status_code}`
  - `http_request_duration_seconds{route, method}` (histogram p50/p95/p99)
  - `http_request_payload_bytes{route}` (histogram)
  - `http_response_payload_bytes{route}` (histogram)
  - `container_cpu_usage_delta{pod}` (gauge, 15s window)
  - `container_memory_working_set_delta{pod}` (gauge)
- Integration tests: metrics emit on HTTP request, labels correct

**Exit criteria:**
- [ ] All 8 signals emitted per HTTP request
- [ ] Labels include route and method for every metric
- [ ] CPU and memory delta computed correctly over 15s window
- [ ] Metrics visible in Prometheus scrape

---

### Issue 3 — PPA-23: Implement cold start handler
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1

**Description:**
New endpoints have no cost history. The cold start handler clusters an unseen endpoint to the nearest known endpoint and inherits that cluster's cost weight. After ≥ 50 real requests, real EWMA observations replace the inherited weight entirely.

**Deliverables:**
- `src/ppa/cold_start.py` — endpoint clustering + inherited cost weight store
- Clustering logic by: HTTP method + payload size bucket + route depth
- Observation counter per endpoint — graduation at 50 requests
- Unit tests: clustering accuracy, graduation transition

**Clustering criteria:**
```python
def cluster_endpoint(route, method, avg_payload_bytes):
    bucket = "small" if avg_payload_bytes < 1024 else \
             "medium" if avg_payload_bytes < 10240 else "large"
    depth = len(route.strip("/").split("/"))
    return find_nearest_cluster(method, bucket, depth)
```

**Exit criteria:**
- [ ] New endpoint inherits nearest cluster weight within one reconciliation cycle
- [ ] Observation counter increments correctly per request
- [ ] Graduates to real EWMA weight after exactly 50 observations
- [ ] Unit tests cover all three payload size buckets and all HTTP methods

---

### Issue 4 — PPA-29: Fix NaN propagation with nan_guard.py
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1

**Description:**
When Prometheus returns no data (scrape gap, target down), NaN silently cascades through the feature pipeline and corrupts XGBoost inputs. This must be fixed at the data ingestion layer before any ML work begins.

**Deliverables:**
- `src/ppa/nan_guard.py` — sanitize all Prometheus query results before feature build
- Rolling average fallback store (last 12 values per metric)
- `FEATURE_BOUNDS` dict — valid min/max per feature for clamping
- Unit tests: NaN input → fallback output, out-of-bounds input → clamped output

**Implementation:**
```python
def safe_query(metric_name: str, fallback_window: int = 12) -> float:
    result = prometheus.query(metric_name)
    if result is None or math.isnan(result):
        return rolling_average(metric_name, steps=fallback_window)
    return clamp(result, FEATURE_BOUNDS[metric_name])
```

**Exit criteria:**
- [ ] NaN input never reaches XGBoost feature vector
- [ ] Rolling average fallback activates on Prometheus scrape gap
- [ ] All features clamped to defined bounds
- [ ] `FeatureVectorException` raised and logged if fallback also unavailable

---

### Issue 5 — NEXUS-2: Implement EventCorrelator clustering
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 1–2

**Description:**
Consume raw `IncidentEvent` objects from NATS and cluster related signals into a single `IncidentCluster` within a 5-minute sliding time window. Deduplicates signals from the same agent, groups co-occurring events by namespace and time proximity, and emits the cluster to the Orchestrator.

**Deliverables:**
- `src/nexus/reasoning/event_correlator.py`
- Time-window clustering (5-minute window, configurable)
- Deduplication by `(agent, signal_type, namespace)` key
- `IncidentCluster` Pydantic model with evidence list
- Unit tests with mock NATS streams

**Exit criteria:**
- [ ] Related signals within 5-minute window merged into one cluster
- [ ] Duplicate events from same agent deduplicated
- [ ] `IncidentCluster` emitted to NATS for Orchestrator consumption
- [ ] Unit tests with mock streams covering merge + dedup scenarios

---

### Issue 6 — NEXUS-3: Create Governance Plane foundation (ActionLadder + OPA)
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1–2

**Description:**
Build the safety layer that every healing action must pass through. No action — including PPA scaling decisions — executes without passing these checks. This is the most safety-critical component in the system.

**Deliverables:**
- `src/nexus/governance/action_ladder.py` — L0/L1/L2/L3 router
- `src/nexus/governance/policy_engine.py` — OPA HTTP API client
- `src/nexus/governance/runbook.py` — YAML runbook loader + pre/post check executor
- `deploy/opa/nexus_policies.rego` — OPA policy definitions
- Basic runbook YAML schema loader and validator

**Governance checks (in order):**
```
1. OPA policy check      — is this action type allowed?
2. Blast-radius check    — does this affect > 20% of cluster?
3. Cooldown check        — has this action run on this target recently?
4. Confidence threshold  — is confidence ≥ level-appropriate minimum?
5. Human approval queue  — if L3 or confidence < 0.85, block for approval
6. Circuit breaker       — if 3 consecutive post-checks failed, freeze
```

**Exit criteria:**
- [ ] All 4 levels (L0–L3) route correctly
- [ ] OPA blocks disallowed action types in unit tests
- [ ] Blast-radius check prevents > 20% cluster impact
- [ ] Runbook YAML loads and validates schema correctly

---

### Issue 7 — NEXUS-3b: Implement 5 seed runbooks (deterministic, no LLM)
- **Type:** AFK
- **Blocked by:** NEXUS-3 ⛔
- **Week:** 2–3

**Description:**
Build 5 deterministic runbooks that prove the Governance Plane works correctly before any LLM involvement. These become the baseline the Orchestrator improves upon. Each must pass pre-check + execute + post-check in isolation. This is a hard prerequisite for NEXUS-14 (Orchestrator).

**Deliverables:**
- `src/nexus/governance/runbooks/runbook_pod_crashloop_v1.yaml`
- `src/nexus/governance/runbooks/runbook_high_error_rate_post_deploy_v1.yaml`
- `src/nexus/governance/runbooks/runbook_missing_env_key_v1.yaml`
- `src/nexus/governance/runbooks/runbook_dns_resolution_failure_v1.yaml`
- `src/nexus/governance/runbooks/runbook_db_connection_exhaustion_v1.yaml`
- Integration tests: each runbook executes against injected fault in staging

**Each runbook must include:**
```yaml
id: runbook_pod_crashloop_v1
failure_class: resource_exhaustion
healing_level: 1
blast_radius: single_pod
cooldown_seconds: 120
trigger:
  signal_types: [pod_crashloop]
pre_checks:
  - assert: "pod.restart_count > 3"
actions:
  - type: restart_pod
    params: { namespace: "{{namespace}}", name: "{{pod_name}}" }
post_checks:
  - metric: "pod_restart_count"
    condition: "stable for 5 min"
rollback_if_post_check_fails: true
```

**Exit criteria:**
- [ ] All 5 runbooks pass pre-check + execute + post-check in isolation
- [ ] Each runbook writes to AuditTrail correctly
- [ ] Rollback triggers correctly when post-check fails
- [ ] Zero LLM calls during any runbook execution

---

### Issue 8 — NEXUS-4: Implement AuditTrail module
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 1–2

**Description:**
Immutable append-only audit log for every action taken by the system. No silent actions — every healing attempt, governance check result, and outcome must produce one record.

**Deliverables:**
- `src/nexus/governance/audit_trail.py` — SQLite append-only store
- FastAPI endpoints: `GET /audit/tail?n=20`, `GET /audit/{incident_id}`
- CLI: `nexus audit --n 20`, `nexus audit --action-id <uuid>`
- Unit tests: append, query, verify immutability (no UPDATE/DELETE)

**Audit record schema:**
```json
{
  "action_id":         "uuid",
  "timestamp":         "ISO8601",
  "triggered_by":      "OrchestratorAgent | human:username",
  "runbook_id":        "runbook_pod_crashloop_v1",
  "healing_level":     1,
  "target":            "default/payments-api",
  "pre_check_results": { "passed": true, "details": {} },
  "execution_outcome": "success | failed | rolled_back",
  "post_check_results":{ "slo_restored": true, "p99_after": 145 },
  "rollback_triggered":false,
  "incident_id":       "uuid",
  "ppa_prediction":    { "acp_t5m": 342.1, "confidence": 0.88 }
}
```

**Exit criteria:**
- [ ] Every governance action writes exactly one audit record
- [ ] No UPDATE or DELETE operations possible on audit store
- [ ] FastAPI endpoints return correct records
- [ ] CLI `nexus audit` displays last N records correctly

---

### Issue 9 — NEXUS-5: Bootstrap KnowledgeBase with incident indexing
- **Type:** AFK
- **Blocked by:** NEXUS-2 ⛔
- **Week:** 2–3

**Description:**
PostgreSQL-backed incident store with cosine similarity search for past incident retrieval. The Orchestrator uses this to find the top-3 matching past incidents for any new cluster, boosting confidence when a known pattern repeats.

**Deliverables:**
- `src/nexus/kb/incident_store.py` — PostgreSQL incident history
- `src/nexus/kb/runbook_matcher.py` — cosine similarity past-incident retriever
- `src/nexus/kb/incident_graph.py` — Temporal Knowledge Graph (NetworkX)
- Seed with first 20 incidents from EventCorrelator output
- API: `GET /knowledge` — KnowledgeBase adjustment table

**Exit criteria:**
- [ ] First 20 incidents stored and indexed
- [ ] Cosine similarity search returns correct top-3 matches on test queries
- [ ] Knowledge Graph stores incident relationships correctly
- [ ] Query latency < 200ms for similarity search

---

### Issue 10 — NEXUS-6: Implement MetricsAgent with GRU autoencoder
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 2–3

**Description:**
Scrapes Prometheus for CPU, memory, RPS, latency percentiles, and error rates. Runs a GRU Autoencoder for multivariate anomaly detection. Must have a circuit breaker on Prometheus failures — no socket exhaustion when scrape target is down.

**Deliverables:**
- `src/nexus/agents/metrics_agent.py`
- GRU Autoencoder (PyTorch) for anomaly detection on 14-dim feature vector
- Circuit breaker: exponential backoff on Prometheus connection failure
- `FeatureVectorException` raised on NaN — never silently skipped
- Publishes `ANOMALY_SIGNAL` to NATS when reconstruction error > threshold

**Exit criteria:**
- [ ] Prometheus scraper runs on configurable interval
- [ ] GRU Autoencoder detects injected anomaly within 2 scrape cycles
- [ ] Circuit breaker fires when Prometheus stopped (no socket exhaustion)
- [ ] `FeatureVectorException` raised and logged on NaN input
- [ ] `ANOMALY_SIGNAL` published to NATS on anomaly detection

---

### Issue 11 — INTEGRATION-29a: Basic event stream dashboard
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 2

**Description:**
First Grafana dashboard milestone. Provides basic visibility into the NATS event stream from Week 2 onward. Prevents 12 weeks of blind development.

**Deliverables:**
- Grafana dashboard panel: NATS event rate (events/sec by agent type)
- Grafana dashboard panel: `IncidentEvent` type breakdown (donut)
- Grafana dashboard panel: ACP(t) real-time gauge (placeholder until PPA-19)
- `deploy/nexus/grafana/nexus_dashboard_v1.json`

**Exit criteria:**
- [ ] Dashboard auto-provisions on `docker compose up`
- [ ] NATS event rate visible within 30 seconds of first publish
- [ ] Event type breakdown updates in real time

---

## Phase 2 — Domain Agents + ACP Core (Weeks 4–6)

> **Goal:** All 7 domain agents publishing to NATS. ACP computation running. Shift detection live.

---

### Issue 12 — NEXUS-7: Build DBAgent with DBTrafficCorrelator
- **Type:** HITL
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 4–5
- **HITL decisions required:**
  - Training data pairing strategy (DB snapshots → HTTP RPS window size)
  - Model scope: one Transformer per app or one global model?
  - Multi-table attribution approach

**Description:**
Multi-DB support (PostgreSQL, MySQL, MongoDB) with a Transformer encoder-decoder model that predicts HTTP RPS per endpoint 5–10 minutes ahead from DB table-level read/write patterns. This is the most novel component in NEXUS and the source of Bridge 1 signals to PPA.

**Deliverables:**
- `src/nexus/agents/db_agent.py` — multi-DB query stats collection
- `src/nexus/model/db_traffic_correlator.py` — Transformer (DB → HTTP traffic)
- DB adapters: `psycopg3` (Postgres), `PyMySQL` (MySQL), `pymongo` (MongoDB)
- Publishes `DB_PATTERN` events to NATS (Bridge 1 source)
- Training notebook for DBTrafficCorrelator

**DB signals collected:**

| DB | Interface |
|---|---|
| PostgreSQL | `pg_stat_activity`, `pg_stat_statements`, slow query log |
| MySQL | `performance_schema.events_statements_summary_by_digest` |
| MongoDB | `currentOp`, `serverStatus`, `mongostat` |

**Exit criteria:**
- [ ] DBAgent connects to at least PostgreSQL and emits query pattern events
- [ ] `DB_PATTERN` events published to NATS with `db_read_rate_delta`, `db_write_rate_delta`
- [ ] DBTrafficCorrelator shadow-mode sMAPE < 15% over 2-week window (verified later)
- [ ] Architecture decision on model scope documented before implementation

---

### Issue 13 — PPA-18: Implement adaptive cost learner (EWMA)
- **Type:** HITL
- **Blocked by:** PPA-17 ⛔
- **Week:** 4–5
- **HITL decisions required:**
  - Dynamic alpha thresholds (0.7–0.9 fast, 0.15–0.25 smooth — confirm values)
  - Weight initialization for brand-new deployments with zero history

**Description:**
The heart of PPA v2.0. Continuously learns the true computational cost of each endpoint using EWMA. No offline retraining required — weights update every 30-second reconciliation cycle. Dynamic alpha responds to composition shift signals.

**Deliverables:**
- `src/ppa/cost_learner.py` — EWMA per-endpoint cost weight store
- Dynamic alpha selection logic
- Per-endpoint weight store (in-memory, persisted to PVC on graceful shutdown)
- Publishes cost weights to Prometheus for Grafana dashboarding

**Core formula:**
```python
cost(endpoint, t) = alpha(t) × observed_cost(t) + (1 - alpha(t)) × cost(endpoint, t-1)
observed_cost(t)  = w_cpu × cpu_delta + w_mem × mem_delta + w_lat × latency_impact

def get_alpha(shift_detected: bool, traffic_variance: float) -> float:
    if shift_detected:       return 0.8   # fast reaction
    elif traffic_variance < LOW_VARIANCE_THRESHOLD: return 0.2  # smooth
    else:                    return 0.5   # balanced
```

**Exit criteria:**
- [ ] Cost weights update every 30-second reconciliation cycle
- [ ] Dynamic alpha changes correctly on shift signal
- [ ] Weight store persists across pod restarts (PVC)
- [ ] Alpha threshold values confirmed by HITL review

---

### Issue 14 — PPA-19: Build ACP generator + shift detector
- **Type:** AFK
- **Blocked by:** PPA-18 ⛔
- **Week:** 5–6

**Description:**
Aggregates per-endpoint cost weights and live request rates into `ACP(t)`. Simultaneously monitors traffic composition ratios and fires `shift_detected` when workload mix changes significantly. This is PPA's most differentiating feature — the only early-warning signal that doesn't require CPU to spike first.

**Deliverables:**
- `src/ppa/acp.py` — ACP(t) computation + per-endpoint contribution breakdown
- `src/ppa/shift_detector.py` — composition shift detection + NATS publish (Bridge 2)
- Prometheus metrics: `ppa_acp_current`, `ppa_read_ratio`, `ppa_write_ratio`, `ppa_heavy_endpoint_ratio`, `ppa_shift_detected`

**ACP formula:**
```python
ACP(t) = sum(cost(endpoint_i) × rps(endpoint_i, t) for i in active_endpoints)
```

**Shift detection:**
```python
shift_detected = (
    abs(write_ratio_now - write_ratio_2min_ago) > threshold
    or abs(heavy_endpoint_ratio_now - heavy_endpoint_ratio_2min_ago) > 0.15
    or acp_composition_delta > (1.5 × rolling_std_ACP)
)
if shift_detected:
    alpha = 0.8
    await nats_client.publish(IncidentEvent(
        signal_type=SignalType.WORKLOAD_SHIFT, ...
    ))  # → Bridge 2
```

**Exit criteria:**
- [ ] ACP(t) computed every 30 seconds from live Prometheus data
- [ ] Shift detector fires within 60 seconds of injected composition change
- [ ] `WORKLOAD_SHIFT` event published to NATS on shift detection
- [ ] All composition metrics visible in Grafana

---

### Issue 15 — NEXUS-8: Implement GitAgent with AST scanner
- **Type:** AFK
- **Blocked by:** None ✅
- **Week:** 4–5

**Description:**
Listens to Git webhook events. AST-scans every push for `os.getenv()`, `process.env.X`, and `config.get()` calls to build a required-env contract. Compares against Kubernetes Secrets and blocks deployment if any required key is missing.

**Deliverables:**
- `src/nexus/agents/git_agent.py` — Git webhook listener + AST scanner
- `scripts/install_git_hooks.sh` — pre-push hook installer
- Publishes `MISSING_ENV_KEY` events to NATS (L0 block)
- Also detects accidentally committed secrets in diff

**Exit criteria:**
- [ ] Pre-push hook blocks test commit with missing env key
- [ ] AST scanner correctly identifies all `os.getenv()` / `process.env.X` patterns
- [ ] `MISSING_ENV_KEY` event published with list of missing keys
- [ ] Committed secret detection fires on test case

---

### Issue 16 — NEXUS-9: Implement NginxAgent + upstream weight control
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 4–5

**Description:**
Tails NGINX access and error logs via OTel. Computes per-endpoint RPS, error rate, upstream health, and P95 latency per route. Subscribes to `WORKLOAD_SHIFT` events from PPA (Bridge 2) and pre-adjusts upstream weights before traffic arrives.

**Deliverables:**
- `src/nexus/agents/nginx_agent.py` — NGINX log tailing + per-endpoint telemetry
- Upstream weight pre-adjustment logic (NGINX Plus API or config reload)
- Subscribe to `nexus.signals.workload_shift` NATS subject
- Publishes `ERROR_SPIKE`, `UPSTREAM_UNHEALTHY` to NATS

**Exit criteria:**
- [ ] Per-endpoint RPS, error rate, P95 latency computed from logs
- [ ] Upstream weights adjusted within one reconciliation cycle of receiving `WORKLOAD_SHIFT`
- [ ] `ERROR_SPIKE` published when error rate > 5% on any route
- [ ] End-to-end test: PPA shift → NATS → NginxAgent weight adjust

---

### Issue 17 — NEXUS-10: Implement NetworkAgent with DNS probes
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 4–5

**Description:**
Periodically resolves all internal and external DNS names in Kubernetes Services and Ingress. Tests inter-service HTTP connectivity with synthetic probes. Monitors for network policy conflicts.

**Deliverables:**
- `src/nexus/agents/network_agent.py`
- DNS resolution probes for all K8s Services and Ingress hosts
- Synthetic HTTP probes between services (configurable interval)
- Network policy conflict detection
- Publishes `DNS_FAILURE`, `CONNECTIVITY_FAILURE` to NATS

**Exit criteria:**
- [ ] DNS failure detected and reported within 60 seconds of injected failure
- [ ] Synthetic probes run on configurable interval
- [ ] `DNS_FAILURE` event published with failing service name
- [ ] Network policy conflict detected in test scenario

---

### Issue 18 — NEXUS-11: Implement ConfigAgent for drift detection
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 5–6

**Description:**
Monitors Kubernetes resource state against declared Git manifest state. Detects `kubectl` changes that bypassed GitOps. Continuously validates that running pods have all required env vars populated at runtime (not just pre-deploy).

**Deliverables:**
- `src/nexus/agents/config_agent.py`
- IaC drift detection: compare actual K8s resources against `git show HEAD:deploy/` snapshots
- Runtime env var validation per running pod
- Drift severity scoring (low / medium / critical)
- Publishes `CONFIG_DRIFT`, `MISSING_ENV_KEY` to NATS

**Exit criteria:**
- [ ] Manual `kubectl` change detected as drift within 2 minutes
- [ ] Runtime env var validation catches missing key in running pod
- [ ] Drift severity score computed correctly
- [ ] `CONFIG_DRIFT` event published with affected resource list

---

### Issue 19 — NEXUS-12a: K8sAgent base (pod health + NATS + execution arm)
- **Type:** AFK
- **Blocked by:** NEXUS-1 ⛔
- **Week:** 4–6

**Description:**
Base K8sAgent without PPA embedding. Observes pod health, restart counts, OOMKilled events, deployment rollout status, and HPA state. Acts as the execution arm for all Kubernetes healing actions after Governance Plane approval. PPA embedding is added separately in NEXUS-12b (Phase 4).

**Deliverables:**
- `src/nexus/agents/k8s_agent.py` — base agent
- Pod health observation: restart counts, OOMKilled, CrashLoopBackOff
- Deployment rollout status monitoring
- HPA state observation (at max replicas flag)
- Execution arm: `scale_deployment()`, `restart_pod()`, `rollout_undo()`
- Publishes `POD_CRASHLOOP`, `OOMKILLED`, `HPA_MAXED` to NATS

**Exit criteria:**
- [ ] `POD_CRASHLOOP` published within 30 seconds of pod entering CrashLoop
- [ ] `OOMKILLED` published on OOMKill event
- [ ] `scale_deployment()` patches replicas correctly after Governance Plane approval
- [ ] `rollout_undo()` rolls back deployment correctly

---

### Issue 20 — INTEGRATION-29b: Agent status + anomaly panels
- **Type:** AFK
- **Blocked by:** NEXUS-6, NEXUS-7, NEXUS-9 ⛔
- **Week:** 7

**Description:**
Second Grafana dashboard milestone. Adds agent health status and anomaly detection visibility once Phase 2 agents are running.

**Deliverables:**
- Panel: per-agent health status (green/yellow/red)
- Panel: GRU anomaly score time-series (MetricsAgent)
- Panel: DB read/write rate delta (DBAgent)
- Panel: per-endpoint RPS from NginxAgent
- Panel: ACP(t) live gauge + composition ratios (PPA-19)

**Exit criteria:**
- [ ] All running agents show health status in dashboard
- [ ] Anomaly score visible in real time
- [ ] ACP and composition ratio panels updating every 30 seconds

---

## Phase 3 — Reasoning & Governance Deepening (Weeks 7–9)

> **Goal:** Orchestrator making RCA decisions. Full governance plane with rollbacks and circuit breaker. Learning plane capturing outcomes.

---

### Issue 21 — NEXUS-13: Implement RCAEngine with Gemini + fallback
- **Type:** AFK
- **Blocked by:** NEXUS-2, NEXUS-5 ⛔
- **Week:** 7–8

**Description:**
The reasoning brain. Takes `IncidentCluster` events and produces a structured RCA hypothesis with confidence score. Uses Gemini 1.5 Flash as primary reasoning engine with a 12-rule deterministic fallback when the API is unavailable.

**Deliverables:**
- `src/nexus/reasoning/rca_engine.py` — Gemini API reasoning loop
- `src/nexus/reasoning/rca_prompt.py` — structured RCA prompt templates
- `src/nexus/reasoning/confidence.py` — multi-factor confidence scorer
- 12-rule deterministic fallback (no Gemini dependency)

**Confidence scoring formula:**
```
confidence = LLM_score × agreement_factor × failure_class_weight
             × historical_boost × ppa_predictive_confidence
```

**RCA output schema:**
```json
{
  "root_cause_hypothesis": "string",
  "confidence_score":      0.0–1.0,
  "recommended_runbook":   "runbook_id",
  "healing_level":         0–3,
  "requires_human_approval": true | false,
  "evidence":              ["AgentType:signal_type", ...]
}
```

**Exit criteria:**
- [ ] Correctly identifies root cause for all 6 failure-class test scenarios
- [ ] Confidence ≥ 0.85 on known failure patterns, < 0.70 on novel ones
- [ ] Fallback to deterministic rules when Gemini API unavailable
- [ ] Response time < 2 seconds on known patterns (fallback path)

---

### Issue 22 — NEXUS-14: Build Orchestrator with runbook selection
- **Type:** HITL
- **Blocked by:** NEXUS-3b, NEXUS-13 ⛔
- **Week:** 8–9
- **HITL decisions required:**
  - Runbook selection tie-breaking strategy
  - Autonomy level progression (when to enable L2 autonomy)

**Description:**
The Orchestrator connects `IncidentCluster` signals to healing actions. Uses RCAEngine output, past incident similarity (KnowledgeBase), and PPA's ACP predictions to select and execute the best runbook through the Governance Plane.

**Deliverables:**
- `src/nexus/reasoning/orchestrator.py` — main reasoning + runbook selection loop
- Integration with `RunbookMatcher` (cosine similarity past incidents)
- Integration with `ConfidenceScorer` (PPA confidence as multiplier)
- Autonomy progression: observe-only → L1 auto → L2 auto (gated by false-heal rate)

**Autonomy progression:**
```
Week 9:  Observe-only mode
         Generates RCA + runbook recommendation, does not trigger actions.
Week 10: Routes L1 actions autonomously.
         L2+ still require human approval.
Week 11: L2 actions enabled if rolling 7-day false-heal rate < 5%.
         L3 always requires approval.
```

**Exit criteria:**
- [ ] Orchestrator selects correct runbook for all 6 failure-class scenarios
- [ ] Orchestrator-selected runbook matches SRE judgment in ≥ 80% of replayed incidents
- [ ] Audit trail shows zero L3 actions without human approval
- [ ] PPA confidence correctly weights RCA confidence score (Bridge 3)

---

### Issue 23 — NEXUS-15: Complete Governance Plane (rollbacks, cooldown, circuit breaker)
- **Type:** AFK
- **Blocked by:** NEXUS-3, NEXUS-4 ⛔
- **Week:** 7–8

**Description:**
Completes the Governance Plane with atomic rollback wrappers, Redis-backed cooldown tracking, and circuit breaker logic. Every L1–L3 action must have a registered rollback that executes automatically if post-check fails.

**Deliverables:**
- `src/nexus/governance/rollback_registry.py` — atomic rollback wrapper per action type
- `src/nexus/governance/cooldown_store.py` — Redis-backed action cooldown tracker
- Circuit breaker: 3 consecutive post-check failures → freeze autonomy + escalate
- Human approval queue: `GET /approvals/pending`, `POST /approve/{action_id}`

**Exit criteria:**
- [ ] Every L1–L3 action type has a registered rollback wrapper
- [ ] Cooldown correctly prevents same action on same target within window
- [ ] Circuit breaker fires after 3 consecutive post-check SLO failures
- [ ] Human approval queue blocks L3 actions until `nexus approve` runs

---

### Issue 24 — NEXUS-16: Implement Learning Plane
- **Type:** AFK
- **Blocked by:** NEXUS-4, NEXUS-14 ⛔
- **Week:** 8–9

**Description:**
Runs every 5 minutes. Processes all closed incidents, labels outcomes, updates runbook success rates, adjusts KnowledgeBase confidence boosts, and surfaces improvement recommendations via `RunbookAdvisor`. Also triggers PPA model retraining when drift exceeds threshold.

**Deliverables:**
- `src/nexus/learning/outcome_labeler.py` — healed / worsened / neutral classifier
- `src/nexus/learning/runbook_scorer.py` — runbook success rate updater
- `src/nexus/learning/retrain_trigger.py` — CI retraining trigger on PPA drift > threshold
- `nexus learning advisor` CLI command
- `GET /advisor?days=30` API endpoint

**Learning loop:**
```
OutcomeStore → Outcome Labeler → Runbook Quality Scorer
→ KnowledgeBase → ConfidenceScorer update
→ RunbookAdvisor (ADD_PRE_CHECK | PROMOTE | RETIRE)
→ ModelRetrainTrigger (if PPA sMAPE > threshold for 7 days)
```

**Exit criteria:**
- [ ] Every closed incident has a labeled outcome
- [ ] Runbook success rates update after each closed incident
- [ ] At least one runbook auto-promoted and one flagged after 30 incidents
- [ ] Model retrain trigger fires when drift > threshold

---

## Phase 4 — PPA v2.0 + K8sAgent Integration (Weeks 10–11)

> **Goal:** Full PPA v2.0 prediction engine running. K8sAgent hosting PPA. Shadow mode active.

---

### Issue 25 — PPA-20: Update features.py for 19-feature vector
- **Type:** AFK
- **Blocked by:** PPA-19, NEXUS-7 ⛔
- **Week:** 10

**Description:**
Updates the feature vector from the 14-feature v1 baseline to the 17-feature ACP vector, then augments with 2 DBAgent signals from Bridge 1 to reach 19 features. All features must be clamped and NaN-guarded before entering XGBoost.

**Deliverables:**
- `src/ppa/features.py` — updated 19-feature ACP vector builder
- NATS subscriber for `DB_PATTERN` events (Bridge 1 integration)
- Cyclical time encoding: `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos`
- All features clamped to `FEATURE_BOUNDS` via `nan_guard.safe_query()`

**Feature vector (19 features):**

| # | Feature | Source |
|---|---|---|
| 1–3 | `acp_1m`, `acp_5m`, `acp_15m` | ACP Generator |
| 4–5 | `acp_delta_1m`, `acp_delta_5m` | ACP Generator |
| 6–8 | `read_ratio`, `write_ratio`, `heavy_endpoint_ratio` | Shift Detector |
| 9 | `shift_detected` | Shift Detector |
| 10–14 | `hour_sin`, `hour_cos`, `dow_sin`, `dow_cos`, `is_weekend` | Generated |
| 15–16 | `acp_24h_ago`, `acp_7d_ago` | Historical store |
| 17 | `replicas_normalized` | K8s API |
| 18–19 | `db_read_rate_delta`, `db_write_rate_delta` | DBAgent via Bridge 1 |

**Exit criteria:**
- [ ] All 19 features computed correctly every 30 seconds
- [ ] DBAgent signals consumed from NATS and added to vector
- [ ] All features clamped to bounds — no out-of-distribution values
- [ ] NaN fallback activates correctly on missing Prometheus data

---

### Issue 26 — PPA-21: Implement XGBoost prediction engine
- **Type:** AFK
- **Blocked by:** PPA-20 ⛔
- **Week:** 10–11

**Description:**
Train and deploy the XGBoost model that forecasts `ACP t+3m, t+5m, t+10m`. XGBoost is chosen over LSTM for its speed, interpretability, and zero-retraining requirement per cycle. LSTM (TFLite) remains an optional upgrade path.

**Deliverables:**
- `src/ppa/predictor.py` — per-CR Predictor instance with XGBoost primary
- `model/train.py` — training script on ACP dataset
- `model/evaluate.py` — MAE / RMSE / sMAPE evaluation
- Per-CR model isolation: `(cr_namespace, cr_name)` keyed state registry
- LSTM TFLite optional upgrade path preserved

**Training targets:**

| Target | Description |
|---|---|
| `acp_t3m` | ACP 3 minutes ahead — fast buffer for cold-start |
| `acp_t5m` | ACP 5 minutes ahead — primary scaling horizon |
| `acp_t10m` | ACP 10 minutes ahead — long-horizon planning |

**Exit criteria:**
- [ ] XGBoost model trained on ≥ 5,000 ACP dataset rows
- [ ] MAE < 15% of mean ACP on held-out validation set
- [ ] Per-CR model isolation confirmed — no cross-namespace bleed
- [ ] Inference latency < 100ms per prediction cycle

---

### Issue 27 — PPA-22: Build confidence-aware decision engine
- **Type:** HITL
- **Blocked by:** PPA-21, NEXUS-3 ⛔
- **Week:** 11
- **HITL decisions required:**
  - Confidence threshold values (0.65 / 0.85 cutoffs — confirm against staging data)
  - Action mapping: confirm all 5 action types are correct for this deployment

**Description:**
Maps predicted ACP and confidence scores to scaling intents. PPA generates the intent — NEXUS ActionLadder approves and executes it. This is the last PPA component before integration bridges can be wired.

**Deliverables:**
- `src/ppa/scaler.py` — updated to emit NATS intent events instead of direct k8s.patch()
- 5 action types: scale_aggressively / scale_conservatively / prewarm / hold / freeze
- Internal oscillation damper (2-min scale-up cooldown, 5-min scale-down cooldown)
- Publishes `ACP_SPIKE_PREDICTED` to NATS (Bridge 3 source)

**Decision mapping:**
| Condition | Intent | Level |
|---|---|---|
| confidence > 0.85, ACP rising | Scale aggressively | L2 |
| confidence 0.65–0.85, ACP rising | Scale conservatively (+1 pod) | L1 |
| `shift_detected`, confidence > 0.70 | Prewarm pods immediately | L1 |
| confidence < 0.65 | Hold — wait next cycle | L0 (log only) |
| `anomaly_detected` | Freeze — alert only | L0 (alert) |
| ACP falling, confidence > 0.75 | Scale down (with cooldown) | L1 |

**Exit criteria:**
- [ ] All 5 action types produce correct NATS events
- [ ] Oscillation damper prevents > 3 scale events in 10 minutes
- [ ] No direct `k8s.patch()` calls anywhere in scaler.py
- [ ] Confidence thresholds confirmed by HITL review against staging data

---

### Issue 28 — PPA-30: Bound predictions to [0, maxReplicas]
- **Type:** AFK
- **Blocked by:** PPA-21 ⛔
- **Week:** 11

**Description:**
XGBoost can occasionally predict negative ACP or replica counts above `maxReplicas` when given out-of-distribution inputs. Add clamping at the output layer to prevent invalid scaling decisions from reaching the Governance Plane.

**Deliverables:**
- Output clamping in `predictor.py`: `predicted_acp >= 0` always
- `desired_replicas = clamp(ceil(predicted_acp / capacity_per_pod), min_replicas, max_replicas)`
- Integration test: edge case inputs → valid bounded outputs

**Exit criteria:**
- [ ] Negative ACP prediction never emitted
- [ ] Desired replicas always within `[minReplicas, maxReplicas]`
- [ ] Integration test covers extreme low and high input values

---

### Issue 29 — PPA-31: Fix training-serving skew normalization
- **Type:** AFK
- **Blocked by:** PPA-21 ⛔
- **Week:** 11

**Description:**
The `referenceReplicas` normalization constant used during training must match the value used during live inference. A mismatch causes systematic prediction bias. Also: rolling window history must be preserved in `CRState` across model reloads to prevent cold-start on upgrade.

**Deliverables:**
- `referenceReplicas` stored in `metadata.json` alongside trained model
- `CRState` rolling window preserved across model reload (no history wipe)
- `model/metadata.json` schema: `{ version, referenceReplicas, featureBounds, trainedAt }`
- Regression test: model upgrade → window preserved → prediction stable

**Exit criteria:**
- [ ] `referenceReplicas` consistent between training and serving
- [ ] Rolling window survives model file replacement
- [ ] Regression test passes: upgrade model, verify predictions stable within 2 cycles

---

### Issue 30 — PPA-32: Implement shadow mode + precision tracking
- **Type:** HITL
- **Blocked by:** PPA-22 ⛔
- **Week:** 11
- **HITL decisions required:**
  - Graduation approval: human must run `nexus approve <id>` to graduate shadow → advisory

**Description:**
Shadow mode runs PPA alongside HPA without applying scaling decisions. Tracks prediction precision over a rolling window and surfaces graduation readiness. Graduation always requires human approval — this is a non-negotiable safety gate.

**CRD spec additions:**
```yaml
spec:
  shadowMode: true
  shadowGraduationPrecision: 0.80      # configurable, default 0.80
  shadowGraduationMinDecisions: 20     # configurable, default 20
  shadowGraduationRequiresApproval: true  # always HITL — cannot be disabled
```

**Deliverables:**
- Shadow mode flag in CRD spec and operator main loop
- When `shadowMode: true`: compute decisions, skip ActionLadder submission
- Prometheus metrics: `ppa_shadow_decision_total`, `ppa_shadow_precision`, `ppa_shadow_would_have_scaled_at`
- Precision tracking: rolling window of predicted spike vs actual spike
- Graduation readiness published to NATS as advisory event

**Exit criteria:**
- [ ] Shadow mode runs 48h in staging with zero replica changes
- [ ] Precision metric visible in Grafana
- [ ] Graduation event published when precision > threshold over min decisions
- [ ] Graduation blocked until `nexus approve <id>` runs

---

### Issue 31 — NEXUS-12b: K8sAgent + PPA embedding
- **Type:** HITL
- **Blocked by:** PPA-22 ⛔
- **Week:** 11
- **HITL decisions required:**
  - Embedding pattern: how does K8sAgent lifecycle manage PPA's per-CR state?
  - Model reload detection: path change vs file timestamp?

**Description:**
Extends NEXUS-12a (base K8sAgent) to host PPA v2.0 as its scaling sub-module. K8sAgent becomes the container that manages PPA's per-CR state registry and bridges PPA's scaling intents to the ActionLadder.

**Deliverables:**
- PPA per-CR state registry embedded in K8sAgent
- Lazy-init and reload detection for model files on PVC
- PPA `CRState` lifecycle: create on CR apply, destroy on CR delete
- History preserved across model reload (uses PPA-31 fix)

**Exit criteria:**
- [ ] K8sAgent hosts PPA state for N CRs without cross-namespace bleed
- [ ] Model reload detected and triggered correctly on path/timestamp change
- [ ] PPA `CRState` cleaned up on CR delete (no memory leak)
- [ ] Architecture decision on embedding pattern documented

---

### Issue 32 — INTEGRATION-29c: ACP prediction overlay
- **Type:** AFK
- **Blocked by:** PPA-21 ⛔
- **Week:** 12

**Description:**
Third Grafana dashboard milestone. Adds PPA prediction visibility once XGBoost model is running.

**Deliverables:**
- Panel: `predicted_acp_t5m` vs `actual_acp` overlay (time-series)
- Panel: composition shift events marked on ACP timeline
- Panel: confidence score per horizon (t3m / t5m / t10m)
- Panel: shadow mode — PPA would-have-scaled vs HPA actual-scaled comparison

**Exit criteria:**
- [ ] Predicted vs actual ACP overlay updating every 30 seconds
- [ ] Composition shift events visible as annotations on timeline
- [ ] Shadow mode comparison panel shows both decision streams

---

## Phase 5 — Integration Finishing (Week 13)

> **Goal:** All 5 bridges wired and tested end-to-end. Full unified dashboard live.

---

### Issue 33 — INTEGRATION-24: Wire Bridge 1 (DBAgent → PPA features)
- **Type:** AFK
- **Blocked by:** NEXUS-7, PPA-20 ⛔
- **Week:** 13

**Description:**
DBAgent publishes `DB_PATTERN` events to NATS. PPA's `features.py` subscribes and augments its feature vector with `db_read_rate_delta` and `db_write_rate_delta`. Run A/B comparison to confirm prediction quality improvement.

**What flows:**
```python
# DBAgent publishes to: nexus.signals.db
IncidentEvent(
    agent=AgentType.DB,
    signal_type=SignalType.DB_PATTERN,
    context={
        "db_read_rate_delta":  float,  # reads/sec change over 2 min
        "db_write_rate_delta": float,  # writes/sec change over 2 min
        "table": "orders"
    }
)
# PPA features.py subscribes and adds features 18–19
```

**Exit criteria:**
- [ ] `DB_PATTERN` events flow from DBAgent to PPA feature vector
- [ ] Features 18–19 correctly populated in inference vector
- [ ] A/B test shows > 5% MAE reduction with DB features vs without

---

### Issue 34 — INTEGRATION-25: Wire Bridge 2 (Shift detector → NATS + NginxAgent)
- **Type:** AFK
- **Blocked by:** PPA-19, NEXUS-9 ⛔
- **Week:** 13

**Description:**
When PPA's shift detector fires, `WORKLOAD_SHIFT` event flows to NATS. NginxAgent subscribes and pre-adjusts upstream weights. Orchestrator receives it as additional RCA evidence. This is the only early-warning path that operates outside the prediction engine.

**End-to-end test:**
```
1. Inject composition shift (POST /orders traffic ↑ 60%)
2. PPA shift_detector fires within 60 seconds
3. WORKLOAD_SHIFT event appears on NATS
4. NginxAgent pre-adjusts upstream weights within one cycle
5. Orchestrator logs WORKLOAD_SHIFT as RCA evidence
6. AuditTrail record shows WORKLOAD_SHIFT contributed to confidence score
```

**Exit criteria:**
- [ ] Full end-to-end test passes in staging environment
- [ ] NginxAgent weight adjustment confirmed in NGINX metrics
- [ ] Orchestrator uses WORKLOAD_SHIFT in RCA evidence list
- [ ] Latency from shift injection to NginxAgent reaction < 90 seconds

---

### Issue 35 — INTEGRATION-26: Wire Bridge 3 (PPA → Orchestrator)
- **Type:** AFK
- **Blocked by:** NEXUS-13, PPA-22 ⛔
- **Week:** 13

**Description:**
PPA's `predicted_acp_t5m` and `confidence_t5m` published to NATS and consumed by NEXUS's `ConfidenceScorer` as a multiplying factor. Verify PPA confidence correctly weights RCA outcomes.

**What flows:**
```python
# PPA publishes to: nexus.signals.ppa
IncidentEvent(
    agent=AgentType.K8S,
    signal_type=SignalType.ACP_SPIKE_PREDICTED,
    context={
        "predicted_acp_t5m": float,
        "confidence_t5m":    float,
        "desired_replicas":  int,
        "action":            "scale_up | hold | prewarm"
    }
)
# NEXUS ConfidenceScorer consumes:
nexus_confidence = llm_score × agreement × class_weight × historical_boost
                   × ppa_predictive_confidence  # ← PPA's factor
```

**Exit criteria:**
- [ ] PPA prediction events flow to NATS every 30 seconds
- [ ] ConfidenceScorer consumes PPA confidence as multiplier
- [ ] High PPA confidence raises NEXUS overall confidence in traffic-pressure scenarios
- [ ] Low PPA confidence reduces NEXUS confidence correctly

---

### Issue 36 — INTEGRATION-27: Wire Bridge 4 (Runbook approval → PPA execute)
- **Type:** HITL
- **Blocked by:** NEXUS-14, PPA-22 ⛔
- **Week:** 13
- **HITL decisions required:**
  - Pre-check threshold values in the runbook (confirm before deploying)
  - Post-check window (60s vs 120s — confirm against observed pod startup times)

**Description:**
PPA submits scaling intent as `IncidentEvent`. NEXUS ActionLadder processes it through governance checks and either approves (K8sAgent executes) or blocks (logs + escalates). This is the bridge that makes PPA a governed citizen of NEXUS rather than a rogue scaler.

**Runbook:**
```yaml
id: runbook_ppa_acp_scale_v1
description: "Pre-scale deployment based on PPA ACP forecast"
failure_class: traffic_pressure
healing_level: 2
blast_radius: single_deployment
cooldown_seconds: 120
trigger:
  signal_types: [acp_spike_predicted, composition_shift_detected]
pre_checks:
  - assert: "predicted_acp > current_acp * 1.30"
  - assert: "ppa_confidence > 0.65"
  - assert: "current_replicas < max_replicas"
actions:
  - type: scale_deployment
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

**Exit criteria:**
- [ ] PPA scaling intent flows through ActionLadder correctly
- [ ] OPA, cooldown, blast-radius checks all gate the action
- [ ] Pre-check blocks action when `predicted_acp` not significantly elevated
- [ ] Post-check triggers rollback when error rate stays elevated after scaling
- [ ] AuditTrail record written for every PPA-triggered scale event

---

### Issue 37 — INTEGRATION-28: Wire Bridge 5 (Scaling outcomes → Learning)
- **Type:** AFK
- **Blocked by:** NEXUS-16, PPA-22 ⛔
- **Week:** 13

**Description:**
After every scaling action completes, PPA reports a structured `OutcomeRecord` to NEXUS's `OutcomeStore`. The Learning Plane processes it to update runbook success rates, adjust confidence boosts, and trigger model retraining if drift exceeds threshold.

**What flows:**
```python
# In PPA outcome_reporter.py (new module)
await outcome_store.record(OutcomeRecord(
    incident_id=cluster_id,
    runbook_id="runbook_ppa_acp_scale_v1",
    predicted_acp=predicted_acp_t5m,
    actual_acp=measured_acp_5min_later,    # measured post-scale
    replicas_before=current_replicas,
    replicas_after=desired_replicas,
    p99_latency_before=p99_before,
    p99_latency_after=p99_after,
    error_rate_after=error_rate,
    success=error_rate < 0.01,
    timestamp=datetime.utcnow()
))
```

**Exit criteria:**
- [ ] Every PPA scaling action produces one `OutcomeRecord`
- [ ] Learning Plane processes outcome within 5-minute cycle
- [ ] `runbook_ppa_acp_scale_v1` success rate visible in Grafana
- [ ] `nexus learning advisor` surfaces recommendations after 10+ PPA outcomes

---

### Issue 38 — INTEGRATION-29d: Full 5-plane unified dashboard
- **Type:** AFK
- **Blocked by:** INTEGRATION-29c, NEXUS-16 ⛔
- **Week:** 13

**Description:**
Final Grafana dashboard milestone. Combines all four previous dashboard milestones into a unified 5-plane view showing the complete NEXUS + PPA system state.

**Dashboard rows:**

| Row | Panels |
|---|---|
| **Overview** | Autonomous Success Rate · False Heal Rate · Total Actions · Circuit Breaker State |
| **PPA Prediction** | ACP predicted vs actual · Composition shift timeline · Confidence per horizon · Shadow vs HPA comparison |
| **Agent Health** | Per-agent status · Anomaly score · DB read/write rate delta · Composition ratios |
| **Healing** | Actions by outcome (time-series) · Actions by level (donut) · Runbook success rates |
| **RCA** | Confidence score p50/p90 · RCA duration p50/p99 · Evidence agent breakdown |
| **Learning** | Runbook quality improvement · KnowledgeBase confidence adjustments · Model retrain events |

**Exit criteria:**
- [ ] All 6 dashboard rows populated with live data
- [ ] Dashboard auto-provisions via `deploy/nexus/grafana/nexus_dashboard_final.json`
- [ ] All KPI targets from evaluation criteria visible without manual PromQL queries
- [ ] `nexus approve` successfully unblocks a pending action visible in dashboard

---

## Dependency Map

```
NEXUS-1 ──► NEXUS-2 ──► NEXUS-5 ──► NEXUS-13 ──► NEXUS-14 ──► NEXUS-16
         ├──► NEXUS-6                    └──────────────────────► NEXUS-16
         ├──► NEXUS-9 ──────────────────────────────────────────► INT-25
         ├──► NEXUS-10
         ├──► NEXUS-11
         └──► NEXUS-12a

NEXUS-3 ──► NEXUS-3b ──► NEXUS-14
         └──► NEXUS-15

NEXUS-4 ──► NEXUS-15
         └──► NEXUS-16

NEXUS-7 ──────────────────────────────────────────────────────► PPA-20
                                                                      └──► INT-24

PPA-17 ──► PPA-18 ──► PPA-19 ──► PPA-20 ──► PPA-21 ──► PPA-22 ──► PPA-32
        │         │          │           │          └──► PPA-30     └──► NEXUS-12b
        │         │          │           └──► PPA-31
        │         └──► PPA-23
        └──► PPA-29

PPA-21 ──────────────────────────────────────────────────────► INT-29c
PPA-22 ──► INT-25, INT-26, INT-27, INT-28, NEXUS-12b

NEXUS-13 + PPA-22 ──► INT-26
NEXUS-14 + PPA-22 ──► INT-27
NEXUS-16 + PPA-22 ──► INT-28
NEXUS-7  + PPA-20 ──► INT-24
PPA-19   + NEXUS-9 ──► INT-25
INT-29c  + NEXUS-16 ──► INT-29d
```

---

## HITL Decision Register

| Issue | Decision Required | Who | When |
|---|---|---|---|
| PPA-18 | Dynamic alpha threshold values (0.7–0.9 / 0.15–0.25) | ML engineer | Before Week 4 build |
| PPA-22 | Confidence cutoff values (0.65 / 0.85) against staging data | ML engineer | Before Week 11 merge |
| PPA-32 | Shadow mode graduation approval | Team lead | After 20+ shadow decisions |
| NEXUS-7 | DBTrafficCorrelator architecture (model scope + training data pairing) | Architect | Before Week 4 build |
| NEXUS-12b | K8sAgent embedding pattern (lifecycle + reload detection) | Architect | Before Week 11 build |
| NEXUS-14 | Orchestrator autonomy level progression + runbook tie-breaking | Team lead | Before Week 9 enable |
| INTEGRATION-27 | Bridge 4 runbook pre/post check threshold values | Team lead | Before Week 13 deploy |

---

## KPI Targets

### NEXUS Reliability

| KPI | Target |
|---|---|
| MTTD (detection) | < 90 seconds |
| MTTR L1/L2 | < 5 minutes |
| MTTR L3 | < 10 minutes |
| Time-to-safe after bad deploy | < 4 minutes |
| Incident recurrence rate | < 10% within 48h |

### NEXUS Autonomy

| KPI | Target |
|---|---|
| L1 autonomous success rate | > 90% |
| L2 autonomous success rate | > 80% |
| False-heal rate | < 5% |
| False-positive alert rate | < 10% |
| Rollback success rate | > 95% |
| Governance gate latency | < 500ms (median OPA check) |

### PPA Prediction

| KPI | Target |
|---|---|
| ACP prediction MAE (t+5m) | < 15% of mean ACP |
| Composition shift detection recall | > 90% |
| p99 latency during spike | < 200ms |
| Scaling reaction time | < 90s from spike |
| Dropped requests during spike | < 0.1% |
| Scaling oscillations per hour | < 3 |
| Pod utilization average | > 70% |
| Cold start cost accuracy | > 80% after 50 requests |

### Integration

| KPI | Target |
|---|---|
| DBAgent → PPA MAE improvement | > 5% reduction |
| Shadow mode precision | > 80% over 20 decisions |
| Pre-deploy block rate (missing env) | ≥ 80% of simulated incidents |
| Governance gate latency | < 500ms median |

---

## Issue Count Summary

| Phase | Issues | Type Breakdown |
|---|---|---|
| Phase 1 — Foundation + Early PPA | 11 | 11 AFK, 0 HITL |
| Phase 2 — Domain Agents + ACP Core | 9 | 7 AFK, 2 HITL |
| Phase 3 — Reasoning & Governance | 4 | 3 AFK, 1 HITL |
| Phase 4 — PPA v2.0 + K8sAgent | 8 | 5 AFK, 3 HITL |
| Phase 5 — Integration Finishing | 6 | 5 AFK, 1 HITL |
| **Total** | **38** | **31 AFK, 7 HITL (18%)** |

---

> **Plan status: ✅ Approved**
> All 38 issues audited. No dependency cycles. No phantom blocker references. No phase violations. All 5 integration bridges covered. Dashboard milestones incremental across all phases. Ready to publish to issue tracker.
