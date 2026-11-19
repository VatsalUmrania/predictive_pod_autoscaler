"""
NEXUS — Neural Execution and X-layer Unified Self-healing

A multi-agent self-healing cloud infrastructure system built on top of the
Predictive Pod Autoscaler (PPA). NEXUS sits at every layer of the stack:
load balancer, git repo, Kubernetes pods, metrics, database, and network.

Canonical Architecture:
    nexus/
    ├── core/          Infrastructure foundations (bus, database, telemetry)
    ├── engine/        Autonomous agent engine (workflow, nodes, reasoning, tools, platforms)
    ├── governance/    Zero-trust security & safety (policies, safeguards, audit, rollback)
    ├── intelligence/  Predictive autoscaling & learning (predictive, learning, knowledge)
    ├── services/      Delivery channels & interfaces (api, notifications, reporting, observability, gateway, cli)
    └── sdk/           Client integration SDKs (python, js)
"""

__version__ = "0.1.0"
__author__ = "NEXUS Contributors"
