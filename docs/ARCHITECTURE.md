---
title: System Architecture
description: High-level architecture of PPA and NEXUS systems
last-updated: 2026-05-16
---

# System Architecture {#architecture}

## Overview {#overview}

This monorepo contains two complementary Kubernetes autoscaling systems:

### PPA (Predictive Pod Autoscaler)
```
CLI (ppa) → Operator (Kopf) → Predictor (TFLite)
                │                   │
                ▼                   ▼
          Prometheus            LSTM Model
          (metrics)            (prediction)
                │                   │
                ▼                   ▼
        Feature Engine    →   Scale Decision
                                    │
                                    ▼
                          K8s Deployment Patch
```

### NEXUS (Self-Healing Infrastructure)
```
SENSE → REASON → ACT → VERIFY → LEARN
 │      │       │      │       │
Agents  RCA   Action  Post-  KnowledgeBase
        Score  Ladder check  OutcomeStore
```

**Key Difference:**
- **PPA** predicts traffic and scales proactively
- **NEXUS** detects failures and heals autonomously
- **Together:** PPA integrates as the Predictive plane within NEXUS

---

## PPA Data Flow {#ppa-data-flow}

### Reconciliation Loop {#reconciliation-loop}

**Source:** `src/ppa/operator/main.py:1448`

Every 30 seconds:
1. Parse CRD spec → config dict
2. Fetch metrics from Prometheus (14 features)
3. Load TFLite model + scaler
4. Predict load (LSTM inference)
5. Calculate target replicas
6. Stabilization check (2 consecutive cycles)
7. Apply scaling (if non-observer)

**See:** [PPA Architecture](PPA_ARCHITECTURE.md) for details

---

## NEXUS Event Flow {#nexus-event-flow}

### Incident Lifecycle {#incident-lifecycle}

**Source:** `src/nexus/agents/`

7 stages:
1. **SENSE** - Domain agents detect anomalies
2. **PUBLISH** - NATS Event Bus (IncidentEvent schema)
3. **CORRELATE** - EventCorrelator groups events (5 min window)
4. **REASON** - RCAEngine (LLM + 12-rule fallback)
5. **GOVERN** - ActionLadder (OPA, cooldown, confidence)
6. **ACT** - Execute approved runbook
7. **LEARN** - OutcomeStore → KnowledgeBase

**See:** [NEXUS Architecture](NEXUS_ARCHITECTURE.md) for details

---

## Component Summary {#component-summary}

### PPA Components
| Component | Responsibility |
|-----------|---------------|
| Operator | Reconciliation loop, CR management |
| Predictor | TFLite inference, history tracking |
| Feature Engine | Prometheus feature extraction |
| Scaler | K8s Deployment patching |

### NEXUS Components
| Component | Responsibility |
|-----------|---------------|
| 7 Domain Agents | Detection (NGINX, Git, K8s, Metrics, DB, Network, Config) |
| Event Bus | NATS JetStream normalization |
| RCA Engine | LLM-powered root cause analysis |
| Governance | ActionLadder L0-L3, OPA policy |
| Learning | OutcomeStore, KnowledgeBase |

---

## Integration Points {#integration}

P PA and NEXUS integrate at:
- **Shared metrics:** Prometheus for feature collection
- **K8s API:** Both scale deployments (PPA predictive, NEXUS reactive)
- **NEXUS Predictive Plane:** PPA output feeds NEXUS RCA confidence

---

## See Also

- [PPA Architecture](PPA_ARCHITECTURE.md) - Detailed PPA internals
- [NEXUS Architecture](NEXUS_ARCHITECTURE.md) - Detailed NEXUS internals
- [Components](COMPONENTS.md) - Component API reference
- [Operations](OPERATIONS.md) - Deployment guide
