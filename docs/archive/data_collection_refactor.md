# PLAN — Data Collection Refactor (Phase 2A)

**Predictive Pod Autoscaler | Semester 6**

## Goal
Make the data collection pipeline 100% reusable and robust by parameterizing all queries, expanding metrics to include saturation and I/O, and containerizing the export process as a Kubernetes CronJob.

## Scope & File Changes

### 1. Parameterization & Central Configuration
- **File**: `data/config.py` [NEW]
- **Details**: 
  - Define `TARGET_APP`, `PROMETHEUS_URL`, `NAMESPACE`, `CONTAINER_NAME` as environment variables.
  - Define `QUERIES` dictionary with parameterized PromQL including 12 features (6 existing + 4 new + 2 derivatives).

### 2. Script Updates
- **File**: `src/ppa/dataflow/verify_features.py` [UPDATE]
- **Details**: 
  - Import `QUERIES` and config from `config.py`.
  - Iterate and verify all features dynamically.
- **File**: `data/export_training_data.py` [UPDATE]
- **Details**: 
  - Import config parameterization.
  - Rely on internal cluster DNS (`PROMETHEUS_URL`) to access Prometheus remotely.
  - Save output based on `OUTPUT_PATH`.

### 3. Containerization & Deployment
- **File**: `deploy/cronjob-data-collector.yaml` [NEW]
- **Details**: 
  - Define CronJob scheduling hourly extraction inside the cluster.
  - Use `PersistentVolumeClaim` to store the training data CSVs safely across occurrences.

## Agent Assignments
- **`backend-specialist`**: Python script refactoring and metrics implementation.
- **`devops-engineer`**: Setting up the internal cluster CronJob, PVC, and networking.

## Verification Checklist
- [ ] Ensure local connection to Prometheus: run `python3 src/ppa/dataflow/verify_features.py`
- [ ] Apply Kubernetes manifests: `kubectl apply -f deploy/cronjob-data-collector.yaml`
- [ ] Perform a manual job test: `kubectl create job --from=cronjob/ppa-data-collector ppa-collect-now`
- [ ] Check logs of the job: `kubectl logs -l job-name=ppa-collect-now -f`
- [ ] Verify CSV storage on the PV/PVC
