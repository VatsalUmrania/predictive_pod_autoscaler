---
title: NEXUS Architecture
description: Detailed internals of the NEXUS self-healing system
last-updated: 2026-05-16
---

# NEXUS Architecture {#nexus-architecture}

## Overview {#overview}

NEXUS moves Kubernetes self-healing from **reactive** to **proactive** by detecting failures before they cascade, healing autonomously at the right blast-radius, and learning from every incident.

**The Core Loop:**

```
SENSE → REASON → ACT → VERIFY → LEARN
 │      │        │       │        │
Agents  RCA   Action   Post-   KnowledgeBase
        Score   Ladder  check   OutcomeStore
```

---

## Architecture Planes {#planes}

| Plane | Responsibility | Key Modules |
|-------|---------------|-------------|
| **1. Telemetry** | Collect signals from all domains | OpenTelemetry, Prometheus, NGINX logs, Git webhooks |
| **2. Event Bus** | Normalize and route all events | NATS JetStream, IncidentEvent schema |
| **3. Agents** | Domain-specific detection | NGINX, Git, K8s, Metrics, DB, Network, Config |
| **4. Reasoning** | Correlate events, analyze root cause | EventCorrelator, RCAEngine, ConfidenceScorer |
| **5. Governance** | Gate all actions with policy | ActionLadder, OPA, cooldown, human approval |
| **6. Learning** | Improve from outcomes | OutcomeStore, KnowledgeBase, RunbookAdvisor |
| **7. Observability** | Expose metrics and status | FastAPI, Prometheus, Grafana, CLI |
| **8. Predictive** | Forecast scaling pressure | PPA integration, ACP forecasting |

---

## Event Flow {#event-flow}

### Incident Lifecycle {#incident-lifecycle}

```
1. SENSE: Domain Agents detect anomalies
      BaseAgent.sense() → List[IncidentEvent]
      - Poll interval: 30s
      - Circuit breaker on failures
      ─────────────┼──────────────
                   ▼
2. PUBLISH: NATS Event Bus
      Subject: nexus.incidents.{agent}.{signal}
      - signal_type, severity, agent_type, context
      ─────────────┼──────────────
                   ▼
3. CORRELATE: EventCorrelator groups events
      Time window: 5 minutes
      Output: IncidentCluster
      ─────────────┼──────────────
                   ▼
4. REASON: RCAEngine analyzes
      Layer 1: LLM (Gemini/OpenAI/Anthropic)
      Layer 2: Rule-based fallback (12 rules)
      Output: RCAResult (failure_class, healing_level 0-3)
      ─────────────┼──────────────
                   ▼
5. GOVERN: ActionLadder evaluates
      OPA checks, cooldown, blast-radius, confidence
      ─────────────┼──────────────
                   ▼
6. ACT: RunbookExecutor executes
      K8s patch, rollback, notification
      Post-check validates SLO restored
      ─────────────┼──────────────
                   ▼
7. LEARN: OutcomeStore records
      FeedbackLoop updates runbook quality scores
```

---

## Domain Agents {#agents}

### Agent Types {#agent-types}

| Agent | File | Detects |
|-------|------|---------|
| NginxAgent | nginx_agent.py | HTTP error spikes, upstream failures |
| GitAgent | git_agent.py | Deploy events, missing env vars |
| K8sAgent | k8s_agent.py | Pod crashes, OOMKilled, restart loops |
| MetricsAgent | metrics_agent.py | SLO violations, traffic anomalies |
| DBAgent | db_agent.py | Connection pool exhaustion, slow queries |
| NetworkAgent | network_agent.py | DNS failures, network partition |
| ConfigAgent | config_agent.py | ConfigMap drift, GitOps violations |

---

## Governance Plane {#governance}

### Action Ladder Levels {#action-ladder}

| Level | Name | Approval | Auto-Rollback |
|-------|------|----------|---------------|
| L0 | Detect + Alert | None | N/A |
| L1 | Restart/Scale ±1 | None (5 min timeout) | Yes |
| L2 | Canary/Scale ±3 | None (confidence ≥ 0.85) | Yes |
| L3 | Rollback/Patch | Human if confidence < 0.85 | Yes |

### Governance Checks {#governance-checks}

1. **OPA policy** — action type allowed
2. **Blast radius** — ≤ 20% cluster affected
3. **Cooldown** — not repeated within N seconds
4. **Confidence floor** — meets level threshold
5. **Human approval** — if L3 or low confidence
6. **Circuit breaker** — freeze after 3 failures

---

## RCA Engine {#rca-engine}

### Failure Classes {#failure-classes}

- `bad_deploy` — Recent deployment caused failure
- `resource_exhaustion` — Memory, CPU, disk
- `dependency_failure` — Upstream service
- `config_error` — Misconfiguration
- `cascading_failure` — Propagated failure
- `unknown` — Unclassified

---

## Configuration {#configuration}

### Environment Variables {#env-vars}

| Variable | Default | Description |
|----------|---------|-------------|
| NEXUS_GEMINI_API_KEY | (none) | Gemini key (optional) |
| NEXUS_NATS_URL | nats://localhost:4222 | NATS endpoint |
| NEXUS_SQLITE_PATH | data/nexus.db | Audit database |

---

## See Also {#see-also}

- [Architecture - NEXUS Event Flow](ARCHITECTURE.md#nexus-event-flow)
- [Components - NEXUS](COMPONENTS.md#nexus-components)
- [CLI Reference - NEXUS Commands](CLI_REFERENCE.md#nexus-commands)
- [Security - Audit Logging](SECURITY.md#audit-logging)
