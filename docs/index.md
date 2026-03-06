# PPA Documentation Index

> Central index for all Predictive Pod Autoscaler technical documentation.

## Core Architecture
- [architecture.md](./architecture.md): The "Hub" document providing a macro-level system overview.
  - [data_collection.md](./architecture/data_collection.md): The metrics pipeline and dynamic HPA scaling mechanism.
  - [ml_operator.md](./architecture/ml_operator.md): The multi-CR Kopf operator and offline LSTM pipeline.
- [Predictive_Pod_Autoscaler_PRD.pdf](./Predictive_Pod_Autoscaler_PRD.pdf): The original Project Requirements Document (PDF).

## Reference Guides (`reference/`)
- [ppa_commands.md](./reference/ppa_commands.md): Extensive operational commands, startup script usage, and cluster debugging steps.
- [working_queries.md](./reference/working_queries.md): PromQL snippets utilized for data extraction and dashboarding.

## Historical Planning (`archive/`)
Old planning, sprint tracking, and architectural decision files have been moved to the archive to keep the active directory clean:
- [ppa_phase2_architecture.md](./archive/ppa_phase2_architecture.md): The initial phase 2 specs.
- [implementation_audit_5_march.md](./archive/implementation_audit_5_march.md): Initial audit logs.
- [data_collection_refactor.md](./archive/data_collection_refactor.md): Refactoring plans.

## Active Subdirectories
- `planning/`: Retained for future active phase planning (currently empty).
- `reference/`: Retained for active command/query storage.
