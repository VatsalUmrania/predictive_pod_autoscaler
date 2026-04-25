"""
NEXUS — Neural Execution and X-layer Unified Self-healing

A multi-agent self-healing cloud infrastructure system built on top of the
Predictive Pod Autoscaler (PPA). NEXUS sits at every layer of the stack:
load balancer, git repo, Kubernetes pods, metrics, database, and network.

Package structure:
    nexus/
    ├── bus/           Normalized incident event schema + NATS JetStream client
    ├── telemetry/     OTel-based log/metric/trace shipping pipelines
    ├── agents/        Domain-specific observability + first-line action agents
    ├── governance/    Runbook executor, audit trail, OPA policy engine, action ladder
    ├── orchestrator/  LLM-backed RCA + healing decision engine (Phase 4)
    ├── kb/            Temporal knowledge graph + incident memory (Phase 4)
    ├── model/         GRU anomaly detector + DBTrafficCorrelator (Phase 5)
    └── learning/      Outcome labeling + runbook quality scoring (Phase 6)
"""

__version__ = "0.1.0"
__author__ = "NEXUS Contributors"
