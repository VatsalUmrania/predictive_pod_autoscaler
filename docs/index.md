# PPA Documentation Index

> Central index for all Predictive Pod Autoscaler technical documentation.

## Core Architecture

### System Overview
- [architecture.md](./architecture.md): The "Hub" document providing a macro-level system overview with diagram.

### Detailed Subsystems
- [data_collection.md](./architecture/data_collection.md): The metrics pipeline and training data generation.
- [ml_pipeline.md](./architecture/ml_pipeline.md): **[NEW v2.1]** Keras LSTM training, multi-horizon forecasting, TFLite conversion, and champion-challenger promotion policy with diagrams.
- **[Operator Documentation](./operator/README.md):** **[NEW v2.1 COMPREHENSIVE]** Complete operator guide with architecture diagrams, deployment, configuration, API reference, commands, and troubleshooting.

### Operator Documentation Folder (`operator/`)
**Comprehensive guides for deploying and managing the PPA operator:**
- [operator/README.md](./operator/README.md): Operator overview and quick start
- [operator/architecture.md](./operator/architecture.md): **Detailed system topology, reconciliation cycle with Mermaid diagrams, component architecture, decision flows, and state machines**
- [operator/deployment.md](./operator/deployment.md): **[NEW]** Comprehensive deployment guide from data collection through live scaling—use this after training data available
- [operator/configuration.md](./operator/configuration.md): Environment variables, CR specification, scaling tuning guide with examples
- [operator/api.md](./operator/api.md): Custom Resource (CR) schema, validation rules, examples, and RBAC requirements
- [operator/commands.md](./operator/commands.md): Useful kubectl commands for monitoring, debugging, and operations
- [operator/troubleshooting.md](./operator/troubleshooting.md): Common issues, error messages, and solutions

### Project Documents
- [Predictive_Pod_Autoscaler_PRD.pdf](./Predictive_Pod_Autoscaler_PRD.pdf): The original Project Requirements Document (PDF).

---

## Command References (`reference/`)

### Comprehensive Guides
- [ppa_commands.md](./reference/ppa_commands.md): Start here — links to all detailed command references.
- **[ml_commands.md](./reference/ml_commands.md):** **[NEW v2.1]** Complete ML training, evaluation, conversion, and promotion commands with examples.
- **[operator_commands.md](./reference/operator_commands.md):** **[NEW v2.1]** Detailed operator deployment, configuration, monitoring, and troubleshooting.

### PromQL Snippets
- [working_queries.md](./reference/working_queries.md): PromQL snippets utilized for data extraction and dashboarding.

---

## Historical Planning (`archive/`)

Old planning, sprint tracking, and architectural decision files have been moved to the archive:
- [ppa_phase2_architecture.md](./archive/ppa_phase2_architecture.md): The initial phase 2 specs.
- [implementation_audit_5_march.md](./archive/implementation_audit_5_march.md): Initial audit logs.
- [data_collection_refactor.md](./archive/data_collection_refactor.md): Refactoring plans.
- [plan-mlModelPipeline.prompt.md](./archive/plan-mlModelPipeline.prompt.md): Original planning prompt.

---

## Quick Start

### 🚀 Deploy Operator (Fastest Path)

After collecting training data:
```bash
./scripts/ppa_redeploy.sh --retrain
kubectl get ppa -w          # Watch status & predictions
kubectl logs -l app=ppa-operator -f  # Watch logs
```

See **[Deployment Guide](./operator/deployment.md)** for step-by-step instructions and troubleshooting.

### 🧠 Train & Promote Models
```bash
python model/pipeline.py \
  --csv data-collection/training-data/training_data_v2.csv \
  --horizons rps_t10m \
  --epochs 50 \
  --promote-if-better \
  --champion-dir model/champions
```

See **[ML Commands](./reference/ml_commands.md)** for details.

---

## Documentation Structure

```
docs/
├── index.md                    ← You are here
├── architecture.md             ← System overview (start here)
├── architecture/
│   ├── data_collection.md     ← Data pipeline
│   ├── ml_pipeline.md         ← [NEW] Training & promotion
│   ├── ml_operator.md         ← [UPDATED] Live operator
│   └── queries.md             ← PromQL details
├── reference/
│   ├── ppa_commands.md        ← Command index (start here for ops)
│   ├── ml_commands.md         ← [NEW] All ML commands
│   ├── operator_commands.md   ← [NEW] All operator commands
│   └── working_queries.md     ← PromQL snippets
└── archive/
    └── (historical planning & decision logs)
```

---

## For Operators / DevOps

1. **Get started:** [Operator Commands](./reference/operator_commands.md) → Quick Start
2. **Understand internals:** [Operator Architecture](./architecture/ml_operator.md)
3. **Troubleshoot:** [Operator Commands](./reference/operator_commands.md) → Troubleshooting

---

## For ML Engineers / Data Scientists

1. **Get started:** [ML Commands](./reference/ml_commands.md) → Quick Start
2. **Understand design:** [ML Pipeline Architecture](./architecture/ml_pipeline.md)
3. **Optimize models:** [ML Commands](./reference/ml_commands.md) → Hyperparameters

---

## For System Architects

1. **Start here:** [architecture.md](./architecture.md) with diagram
2. **Deep dive:** [Data Collection](./architecture/data_collection.md), [ML Pipeline](./architecture/ml_pipeline.md), [Operator](./architecture/ml_operator.md)
3. **Reference:** [Working Queries](./reference/working_queries.md)
