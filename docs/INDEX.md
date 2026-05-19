---
title: PPA & NEXUS Documentation
description: Master navigation for Predictive Pod Autoscaler and NEXUS self-healing infrastructure
last-updated: 2026-05-16
---

# PPA & NEXUS Documentation

## System Overview

### Predictive Pod Autoscaler (PPA) {#ppa-overview}
PPA is an ML-driven horizontal pod autoscaler for Kubernetes that uses LSTM neural networks to forecast traffic 3-10 minutes ahead and proactively scale deployments.

**Key capabilities:**
- LSTM-based traffic prediction (TFLite models, <100ms inference)
- Multi-model aggregation (observer + active scaler pattern)
- Concept drift detection with retraining triggers
- Prometheus integration for metrics collection
- Circuit breaker for metric failures

### NEXUS {#nexus-overview}
NEXUS is a self-healing infrastructure layer that moves Kubernetes from **reactive** to **proactive** — detecting failures before they cascade, healing autonomously, and learning from every incident.

**The core loop:** `SENSE → REASON → ACT → VERIFY → LEARN`

**Key capabilities:**
- **8 Planes:** Telemetry, Event Bus, Agents, Reasoning, Governance, Learning, Observability, Predictive
- **Domain Agents:** NGINX, Git, K8s, Metrics, DB, Network, Config (7 specialist agents)
- **LLM-powered RCA:** Gemini/OpenAI/Anthropic with 12-rule fallback
- **Action Ladder:** L0-L3 graduated response with OPA policy gates
- **Audit trail:** Immutable SQLite database for compliance

---

## Quickstart Paths

| Role | Start Here | Key Tasks |
|------|------------|-----------|
| **Developer** | [Developer Guide](DEVELOPER.md) | Local setup, testing, debugging |
| **Operator** | [Operations Guide](OPERATIONS.md) | Deploy, configure, troubleshoot |
| **End User** | [Operations Guide - App Onboarding](OPERATIONS.md#application-onboarding) | Enable scaling for apps |

---

## Documentation Index

| Document | Description | Audience |
|----------|-------------|----------|
| [Architecture](ARCHITECTURE.md) | System architecture, data flows | All |
| [PPA Architecture](PPA_ARCHITECTURE.md) | PPA internals: operator, predictor, ML pipeline | Dev, Ops |
| [NEXUS Architecture](NEXUS_ARCHITECTURE.md) | NEXUS internals: agents, RCA, event bus | Dev |
| [Components](COMPONENTS.md) | Component reference: APIs, entry points | Dev |
| [Operations](OPERATIONS.md) | Installation, configuration, troubleshooting | Ops |
| [Developer Guide](DEVELOPER.md) | Development workflow, testing, debugging | Dev |
| [CLI Reference](CLI_REFERENCE.md) | Complete CLI command reference | All |
| [Security](SECURITY.md) | Authentication, RBAC, secrets | Ops, Security |

---

## Version Information

| Component | Location | Description |
|-----------|----------|-------------|
| PPA Core | `src/ppa/` | Predictive autoscaler operator and CLI |
| NEXUS | `src/nexus/` | Self-healing layer |
| Operator | `src/ppa/operator/main.py` | Kopf-based K8s operator |
| ML Models | `model/` | Pre-trained TFLite models |

---

## Related Documentation

- **CLAUDE.md** - Project context and conventions
- **deploy/** - Kubernetes manifests
- **demo/** - Example applications
