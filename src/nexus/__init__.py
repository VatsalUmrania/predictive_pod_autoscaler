"""
NEXUS — Neural Execution and X-layer Unified Self-healing

A multi-agent self-healing cloud infrastructure system built on top of the
Predictive Pod Autoscaler (PPA). NEXUS sits at every layer of the stack:
load balancer, git repo, Kubernetes pods, metrics, database, and network.

Package structure:
    nexus/
    ├── bus/           Normalized incident event schema + NATS JetStream client
    ├── cli/           Click-based ``nexus server start`` operator entry point
    ├── agents/        Domain-specific observability + first-line action agents
    ├── governance/    Runbook executor, audit trail, OPA policy engine, action ladder
    ├── integration/   Slack notifier, dashboard, SDK ingestion, selfheal config
    ├── learning/      Outcome labeling, knowledge base, feedback loop, PPA tracker
    ├── observability/ Status API (FastAPI) + Prometheus metrics
    ├── predictive/    Pre-scaler + anomaly detector + DB traffic correlator
    ├── reasoning/     LLM-backed RCA, event correlator, orchestrator
    └── sdk/           Client SDKs (python + js)
"""

__version__ = "0.1.0"
__author__ = "NEXUS Contributors"
