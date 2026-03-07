# Overview
The current data collection pipeline for the Predictive Pod Autoscaler (PPA) captures high-resolution application metrics, but it lacks realism and forward-looking horizons suitable for Kubernetes cold starts. This enhancement improves the data collection methodology to capture chaotic traffic, fixed replica profiles for purer correlations, and forward-shifted target values (predicting T+3 mins).

# Project Type
BACKEND

# Success Criteria
- [ ] The export pipeline correctly shifts metrics by T+3 minutes to create a realistic prediction target.
- [ ] Locust tests include chaotic spikes and drops rather than purely smooth sine waves.
- [ ] Test scenarios include fixed-replica baseline collection to remove HPA feedback loops.
- [ ] Scalability constraints are captured by testing up to max capacity.

# Tech Stack
- Python (Data Export and Locust testing)
- Kubernetes/Prometheus (Metrics Collection)

# File Structure
Existing files to modify/add:
- `data-collection/export_training_data.py`: Update target feature generation.
- `test-app/locustfile.py`: Add chaotic load test profiles.
- `scripts/fixed_replica_test.sh` (new): Automate running load tests with HPA disabled.

# Task Breakdown
- **Task 1: Shift Prediction Horizon to T+3 mins**
  - **Agent**: `backend-specialist`
  - **Skills**: `python-patterns`
  - **INPUT**: `export_training_data.py`
  - **OUTPUT**: Updated script where the generated training CSV maps current features to future capacity requirements at T+3 mins (or adjustable horizon) to account for K8s pod spin-up times.
  - **VERIFY**: Check the output CSV to ensure the target column aligns with timestamps exactly 3 minutes in the future.

- **Task 2: Inject Chaotic Traffic Profiles in Locust**
  - **Agent**: `backend-specialist`
  - **Skills**: `testing-patterns`, `python-patterns`
  - **INPUT**: `locustfile.py`
  - **OUTPUT**: New `LoadTestShape` classes (e.g., `SpikyLoadShape`) that generate flash-crash/flash-spike traffic to mimic real-world unpredictability.
  - **VERIFY**: Run Locust visually or locally plot the user generation curve to ensure sudden 10x traffic spikes.

- **Task 3: Create Fixed Replica Data Collection Scenarios**
  - **Agent**: `devops-engineer`
  - **Skills**: `bash-linux`, `deployment-procedures`
  - **INPUT**: Test bash scripts
  - **OUTPUT**: A `fixed_replica_test.sh` script to disable HPA, scale deployment to fixed N replicas (e.g., 5, 20, 50), and run traffic to record pure RPS/CPU/Latency non-linear correlations.
  - **VERIFY**: Validate metrics from Prometheus while HPA is disabled during a test run.

- **Task 4: Add Simulated External Dependency Metrics (Optional)**
  - **Agent**: `backend-specialist`
  - **Skills**: `api-patterns`
  - **INPUT**: `test-app` instrumentation & Prometheus config
  - **OUTPUT**: Simulated external dependency latency or connection pool saturation metric to train the autoscaler on "when NOT to scale" (e.g. scaling won't solve a DB outage).
  - **VERIFY**: Check Prometheus for the new simulated downstream metric.

## ✅ PHASE X COMPLETE
- Lint: [x] Pass (auto-fixed some surface issues, module warnings remain natively)
- Security: [x] No critical issues (per run)
- Build: [x] Success
- Date: 2026-03-07
