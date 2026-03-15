# PPA Production Roadmap

**Current State:** Proof-of-concept — single app (`test-app`), single cluster, offline-trained LSTM model, manual deployments.  
**Target:** General-purpose predictive autoscaler that any team can onboard a new app onto with a single CRD and let run unsupervised.

---

## Readiness Summary

| Category | Now | Target |
|---|---|---|
| Multi-app support | ❌ Hardcoded `test-app` | ✅ Per-app model registry |
| Model retraining | ❌ Manual offline | ✅ Automated weekly pipeline |
| Operator stability | ⚠️ Single pod, no HA | ✅ Leader-election, health probes |
| Data pipeline | ❌ Manual CSV export | ✅ Streaming metrics collection |
| Monitoring & alerting | ⚠️ Dashboard only, no alerts | ✅ Alert rules + SLO tracking |
| GitOps | ❌ Manual kubectl | ✅ ArgoCD / Flux CD |
| Multi-cluster | ❌ Minikube only | ✅ Cross-cluster model sharing |
| Security | ❌ Broad RBAC | ✅ Least-privilege, secrets rotation |

---

## Phase 1 — Multi-App Support
**Goal:** Any team can onboard a new app by creating a `PredictiveAutoscaler` CR and running one command.  
**Effort:** ~2 weeks

### 1.1 Generalize Model Training

Currently `model/pipeline.py` trains on a fixed CSV. Make it app-aware:

- Add `--app-name` flag to `pipeline.py` and `export_training_data.py`
- Output models to `model/champions/{app-name}/rps_{t3m,t5m,t10m}/`
- Store eval metrics in `model/champions/{app-name}/eval_summary.json`

```bash
python model/pipeline.py \
  --app-name payments-api \
  --csv data-collection/training-data/payments-api.csv \
  --horizons rps_t3m,rps_t5m,rps_t10m \
  --promote-if-better \
  --champion-dir model/champions
```

### 1.2 Model Registry on PVC

Replace the flat `/models/` layout with a namespaced registry:

```
/models/
  test-app/
    rps_t3m/   ppa_model.tflite  scaler.pkl  target_scaler.pkl  eval.json
    rps_t5m/   ...
    rps_t10m/  ...
  payments-api/
    rps_t10m/  ...
```

Update `ppa_redeploy.sh` to accept `--app-name` and upload to the correct path.

### 1.3 CR Templating

Add a `deploy/templates/predictiveautoscaler.yaml.tpl` so new apps don't need to write YAML manually:

```bash
./scripts/onboard_app.sh \
  --app payments-api \
  --namespace billing \
  --deployment payments-api-v2 \
  --min-replicas 2 \
  --max-replicas 20 \
  --rps-capacity 80
```

This generates and applies 3 CRs (t3m + t5m as observers, t10m as active) automatically.

---

## Phase 2 — Continuous Retraining Pipeline
**Goal:** Models retrain automatically on fresh production data. PPA stays accurate as traffic patterns evolve.  
**Effort:** ~2 weeks

### 2.1 Automated Data Collection CronJob

Replace the manual `export_training_data.py` command with a CronJob that runs nightly:

- Already scaffolded in `deploy/cronjob-data-collector.yaml` — needs to write per-app CSVs to a shared volume or object storage (S3/MinIO)
- Add deduplication and append logic so each run extends the dataset rather than overwriting it

### 2.2 Retraining CronJob

Add a weekly retraining CronJob that:

1. Loads the latest CSV from storage
2. Runs `model/pipeline.py` with `--promote-if-better`
3. If the new champion beats the current one (lower sMAPE), uploads to PVC registry
4. Restarts the operator to load the new model (or hot-reload via a spec annotation bump)

```yaml
# deploy/cronjob-retrain.yaml
schedule: "0 2 * * 0"   # Every Sunday 2am
```

### 2.3 Model Version Tracking

Add a `modelVersion` field to the CR status so you can see which model is running:

```yaml
status:
  modelVersion: "2026-03-09-sMAPE-6.72"
  modelLoadedAt: "2026-03-10T14:32:00Z"
  lastRetrainedAt: "2026-03-09T02:00:00Z"
```

---

## Phase 3 — Operator Production Hardening
**Goal:** Operator is reliable enough to run unsupervised in a production cluster.  
**Effort:** ~2 weeks

### 3.1 High Availability

- Enable `kopf` leader election (`--liveness` + `--peering`) so you can run 2 replicas without double-scaling
- Add `livenessProbe` and `readinessProbe` to `deploy/operator-deployment.yaml`

### 3.2 Graceful Degradation

Currently if the model fails to load, the operator logs an error and skips — but HPA keeps running. Make this more robust:

- Add a `fallbackMode` field: if model load fails N times, set `observerMode=true` automatically (don't scale, let HPA handle it)
- Emit a Kubernetes Event when entering fallback mode

### 3.3 Rate Limit Tuning

Current `maxScaleStep` is a fixed config value. Make it dynamic:

- Allow `maxScaleStep` to be overridden per CR in `spec`
- Add a `scaleDownDelay` field to avoid thrashing on traffic troughs

### 3.4 Metrics Hardening

- Add histogram metrics for prediction latency and inference time
- Add `ppa_model_age_seconds` gauge so you can alert on stale models

---

## Phase 4 — Monitoring, Alerting & SLOs
**Goal:** Know immediately when PPA is degrading performance, not 30 minutes later.  
**Effort:** ~1 week

### 4.1 Prometheus Alert Rules

Create `deploy/ppa-alert-rules.yaml` with alerts for:

| Alert | Condition | Severity |
|---|---|---|
| `PPAModelStale` | `ppa_model_age_seconds > 7 * 86400` | warning |
| `PPAHighSkipRate` | `ppa_consecutive_skips > 5` | warning |
| `PPAModelLoadFailed` | `ppa_model_load_failed == 1` | critical |
| `PPAPredictionDrift` | `abs(ppa_predicted_load_rps - actual_rps) / actual_rps > 0.4` over 10m | warning |
| `PPAUnderProvisioned` | `ppa_desired_replicas < kube_deployment_status_replicas_ready` for 5m | critical |

### 4.2 SLO Dashboard Row

Add a dedicated SLO row to the Grafana dashboard:

- **Error Budget Burn Rate** — how fast the 1% error SLO is being consumed
- **Under-provisioning rate (%)** — % of cycles where PPA desired < actual needed
- **Prediction accuracy (1h rolling sMAPE)** — live sMAPE computed from `ppa_predicted_load_rps offset Xm` vs actual

### 4.3 Model Drift Detection

Use the offset accuracy panel (already in dashboard panel 24) as a signal:

- If rolling sMAPE > 15% for more than 1 hour → trigger an early retraining job instead of waiting for the weekly schedule

---

## Phase 5 — GitOps & Multi-Cluster
**Goal:** All deployments and model uploads are driven by Git rather than `kubectl` commands.  
**Effort:** ~2 weeks

### 5.1 ArgoCD / Flux CD Application

- Move all `deploy/` manifests into a Git-driven sync
- Model uploads become a GitOps operation: committing a new TFLite file triggers a pipeline that uploads to the PVC and bumps a spec annotation on the CR to hot-reload

### 5.2 Multi-Cluster Model Sharing

- Store champion models in a shared object store (e.g. MinIO or S3) instead of a PVC
- Each cluster's operator pulls from the registry at startup
- Model promotion in one cluster propagates to all clusters automatically

### 5.3 Onboarding Self-Service

- Build a simple CLI (`ppa onboard <app-name>`) that:
  1. Reads HPA config for the app
  2. Exports historical metrics automatically
  3. Trains a model
  4. Applies 3 CRs (observer + active)
  5. Opens the app's Grafana dashboard

---

## Milestones

```
March 2026   Phase 1 — Multi-app support, per-app model registry
April 2026   Phase 2 — Automated retraining pipeline (CronJobs)
April 2026   Phase 3 — Operator HA, fallback mode, metrics hardening
May 2026     Phase 4 — Alert rules, SLO dashboard, drift detection
June 2026    Phase 5 — GitOps, multi-cluster, self-service CLI
```

---

## Quick Wins (can do today)

1. **`--app-name` flag in `export_training_data.py`** — 30 min change, unblocks Phase 1
2. **`liveness/readinessProbe` in `operator-deployment.yaml`** — 15 min, instant reliability improvement
3. **`ppa-alert-rules.yaml` with 3 critical alerts** — 1 hour, immediately actionable
4. **`maxScaleStep` override in CR spec** — 30 min, allows per-app tuning without redeploying operator
