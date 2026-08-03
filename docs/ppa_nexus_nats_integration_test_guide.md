## Running the Shop-Demo + NEXUS + PPA Stack

The guide outlines **9 sequential phases**. Here's the condensed, practical version:

### 🔑 Prerequisites
```bash
cd /run/media/vatsal/DATA/Projects/predictive_pod_autoscaler
source venv/bin/activate

# Verify tools are ready
docker info | head -3 && minikube version && helm version && kubectl version --client
```

> Note: the only remaining `ppa` CLI command is `ppa model push` (it orchestrates a loader pod for TFLite transfers into the `ppa-models` PVC). Everything below is plain `kubectl` / `docker` / `helm`.

---

### Phase 1 — Deploy NEXUS into K8s
```bash
# PPA CRD + RBAC + persistent volume claim (declared up front so `ppa model push`
# never has to create the PVC imperatively — eliminates the
# "missing kubectl.kubernetes.io/last-applied-configuration annotation"
# warning from Phase 5; the RBAC binding prevents
# `serviceaccount "ppa-operator" not found` from the operator deployment)
kubectl apply -f deploy/crd.yaml
kubectl apply -f deploy/rbac.yaml
kubectl apply -f deploy/ppa-models-pvc.yaml

# NEXUS (NATS + nexus-api)
kubectl apply -f deploy/nexus/nats-statefulset.yaml
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml
kubectl get pods -n nexus   # Wait for: nats-0 Running + nexus-api-xxxx Running
```

---

### Phase 2 — Bootstrap PPA Infra
```bash
# 1. Start minikube
minikube start --cpus=4 --memory=6g --driver=docker

# 2. Enable K8s addons (for HPA + internal traffic)
minikube addons enable metrics-server
minikube addons enable ingress

# 3. Install Prometheus + Grafana via Helm (kube-prometheus-stack)
kubectl create namespace monitoring
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install prometheus prometheus-community/kube-prometheus-stack \
    -n monitoring \
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false
# Wait for Prometheus before moving on — the operator (Phase 5) queries it
# on startup. If it isn't up, the operator trips a circuit breaker and needs
# ~5 min to recover on its own.
kubectl wait --for=condition=ready pod -l "release=prometheus" -n monitoring --timeout=300s

# 4. Build & deploy shop-demo (target app) into minikube's docker daemon
eval $(minikube docker-env)
docker build -t shop-demo:latest -f demo/backend/Dockerfile .
kubectl apply -f demo/backend/deployment.yaml
kubectl get pods -l app=shop-demo -w

# 5. (Optional) Deploy synthetic traffic generator
kubectl apply -f deploy/traffic/traffic-gen-deployment.yaml
```

---

### Phase 3 — Register shop-demo with PPA
Apply the three `PredictiveAutoscaler` CRs directly (one per horizon).
Source files at `deploy/generated-manifests/shop-demo/ppa-normalized_rps_t{3,5,10}m.yaml`:
```bash
cat <<EOF | kubectl apply -f -
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t3m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t3m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: true
  modelPath: "/models/shop-demo/normalized_rps_t3m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t3m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t3m/current/target_scaler.pkl"
---
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t5m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t5m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: true
  modelPath: "/models/shop-demo/normalized_rps_t5m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t5m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t5m/current/target_scaler.pkl"
---
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata: {name: shop-demo-ppa-normalized-rps-t10m, namespace: default}
spec:
  targetDeployment: "shop-demo"
  appName: "shop-demo"
  horizon: "normalized_rps_t10m"
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 50
  scaleUpRate: 2.0
  scaleDownRate: 0.5
  safetyFactor: 1.15
  observerMode: false
  modelPath: "/models/shop-demo/normalized_rps_t10m/current/model.tflite"
  scalerPath: "/models/shop-demo/normalized_rps_t10m/current/scaler.pkl"
  targetScalerPath: "/models/shop-demo/normalized_rps_t10m/current/target_scaler.pkl"
EOF

kubectl get predictiveautoscalers -n default
# Expected:
# shop-demo-ppa-normalized-rps-t3m
# shop-demo-ppa-normalized-rps-t5m
# shop-demo-ppa-normalized-rps-t10m
```

Or, equivalently: `kubectl apply -f deploy/generated-manifests/shop-demo/`.

> **Re-run warning**: if you previously deployed older CRs (e.g. named
> `shop-demo-rps-t3m` with `horizon: "rps_t3m"`) whose model bundles no
> longer exist, multiple autoscalers will fight for the same deployment
> and the operator will log
> `Model bundle pending: no model bundle found for shop-demo/rps_t*` plus
> `Multiple CRs detected for target default/shop-demo; active autoscalers
> can fight over replicas`. Delete the stale ones first:
> ```bash
> kubectl get predictiveautoscalers -n default   # list
> kubectl delete predictiveautoscaler <stale-name> -n default
> ```

---

### Phase 4 — Push pre-trained model to PVC
> No training step — using the pre-trained model from `artifacts/shop-demo/`.
> `ppa model push` spawns a loader pod, runs scaler-regeneration in-cluster,
> and copies `model.tflite` + scalers into the `ppa-models` PVC.
```bash
ppa model push \
  --app-name shop-demo \
  --namespace default \
  --horizon normalized_rps_t3m,normalized_rps_t5m,normalized_rps_t10m
```
> Horizons must match the `appName`/`horizon` in the CR (Phase 3) and the
> model directory layout (`/models/<app>/<horizon>/current/`).

---

### Phase 5 — Build & Deploy the PPA Operator
```bash
# Build operator image in minikube's docker daemon (so the cluster can pull it)
eval $(minikube docker-env)
docker build -t ppa-operator:latest -f src/ppa/operator/Dockerfile .

# Apply operator + verify roll-out
kubectl apply -f deploy/operator-deployment.yaml
kubectl rollout status deployment/ppa-operator -n default --timeout=120s

# Verify NATS_URL is correct:
kubectl get deployment ppa-operator -o jsonpath='{.spec.template.spec.containers[0].env}' \
  | python -m json.tool | grep -A2 NATS_URL
# Must show: "nats://nats.nexus.svc.cluster.local:4222"
```

---

### Phase 6 — Verify Live Events
```bash
# Port-forward NATS (keep this running in a separate terminal)
kubectl port-forward -n nexus svc/nats 4222:4222 8222:8222 &

# Check stream has messages after ~30s
sleep 3 && curl -s "http://localhost:8222/jsz?streams=true" | python -m json.tool | grep -E '"name"|"messages"'

# Watch operator logs for published events
kubectl logs -f deployment/ppa-operator | grep "PPA-NATS"
```

---

### Phase 7 — Verify NEXUS Prescaler is Receiving
```bash
kubectl logs -n nexus deployment/nexus-api -f | grep -i "prescal\|ppa\|prediction"
```
**Expected:**
```
[PreScaler] Subscribed to ppa.predictions.*
[Prescaler] Decision A1B2 [SHADOW] deployment=shop-demo ...
[Prescaler] [SHADOW] Would scale shop-demo 2 → 5 (not executed — shadow mode)
```

---

### ⚡ Fast Verification — Inject a Synthetic Event
> Skip waiting 30s for the real operator cycle — inject a fake PPA event directly:

```bash
# Ensure port-forward is running first (Phase 6), then:
python - <<'EOF'
import asyncio, json, nats
from datetime import datetime, timezone

async def inject():
    nc = await nats.connect("nats://localhost:4222")
    js = nc.jetstream()
    payload = {
        "deployment": "shop-demo", "namespace": "default",
        "predicted_rps": 200.0, "current_rps": 100.0,
        "confidence": 0.82, "horizon_minutes": 10,
        "model_version": "test-inject-v1",
        "raw_features": {"cpu_usage": 0.72, "memory_usage": 0.65, "rps": 100.0, "error_rate": 0.01},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    ack = await js.publish("ppa.predictions.shop-demo", json.dumps(payload).encode(), stream="PPA_PREDICTIONS")
    print(f"✓ Published seq={ack.seq}")
    await nc.drain()

asyncio.run(inject())
EOF

# Then check NEXUS received it
NEXUS=http://$(minikube ip):30081
sleep 2 && curl -s $NEXUS/ppa/decisions | python -m json.tool
```

---

### Phase 8 — Query the NEXUS API
```bash
NEXUS=http://$(minikube ip):30081

curl -s $NEXUS/ppa/stats    | python -m json.tool   # prescaler stats
curl -s $NEXUS/ppa/decisions | python -m json.tool  # decision log
open $NEXUS/docs                                      # Swagger UI
```

---

### Phase 9 — Test LLM Self-Healing + Slack Approval Workflow
> The Gemini RCA engine diagnoses anomalies. **L3 / LLM-sourced remediations
> are staged for human approval** and surfaced in Slack as interactive messages
> with **Approve ✅** / **Reject ❌** buttons; a click POSTs to `/slack/interactive`,
> which verifies Slack's request signature and re-dispatches the decision through
> the governance plane (cooldown / circuit breaker / audit / rollback still apply).
> Full setup + both paths: [docs/nexus_slack_approval_workflow.md](nexus_slack_approval_workflow.md).

#### 9a. Slack app config (one-time)
1. <https://api.slack.com/apps> → **Create New App** → From scratch.
2. **OAuth & Permissions → Bot Token Scopes**: `incoming-webhook`, `chat:write`.
   Install to your workspace/channel → copy the **Webhook URL**
   (`https://hooks.slack.com/services/...`). This is `SLACK_WEBHOOK`.
3. **Features → Interactivity & Shortcuts → ON**. Set **Request URL** to your
   public callback after the ngrok step below:
   `https://<subdomain>.ngrok-free.app/slack/interactive`.
4. **Basic Information → App Credentials** → copy the **Signing Secret**
   (`SLACK_SIGNING_SECRET`). Without it, `/slack/interactive` 401s every click
   (secure-by-default — the raw callback URL alone must never approve a heal).

#### 9b. Expose the callback with ngrok + set the env on the pod
```bash
# Tunnel the nexus-api external NodePort out to a public HTTPS URL.
ngrok http $(minikube ip):30081
#  (running nexus-api locally via uvicorn instead?  →  ngrok http 8080)
#  Copy the https://<subdomain>.ngrok-free.app forwarding URL → set the Slack
#  Interactivity Request URL to https://<subdomain>.ngrok-free.app/slack/interactive,
#  then put the same value in SLACK_INTERACTIVE_URL below.
```

Set the Slack + LLM env on the deployment. Both Slack senders (the governed
Notifier button messages *and* chatops diagnosis messages) resolve through a
single `SLACK_WEBHOOK` — a per-app `selfheal.yaml` override wins when present,
else the env var is the global fallback (so notifications work without mounting
a selfheal.yaml). No `SLACK_WEBHOOK_URL` anymore.
```bash
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml  # incl. RBAC (see 9e)

kubectl set env deployment/nexus-api -n nexus \
  NEXUS_LLM_API_KEY="your_gemini_api_key" \
  SLACK_WEBHOOK="https://hooks.slack.com/services/T000/B000/XXXX" \
  SLACK_SIGNING_SECRET="your_slack_signing_secret" \
  SLACK_INTERACTIVE_URL="https://<subdomain>.ngrok-free.app/slack/interactive"

kubectl rollout status deployment/nexus-api -n nexus --timeout=60s
NEXUS=http://$(minikube ip):30081
```
> ⚠️ Verify `SLACK_INTERACTIVE_URL` has a **single** `https://` (a doubled
> `https://https://…` silently breaks button callbacks). And keep `SLACK_SIGNING_SECRET`
> set — without it `/slack/interactive` 401s every click by design.

#### 9c. Trigger an LLM diagnosis (diagnosis-only — no buttons)
The alertmanager webhook → `LLMOrchestrator.handle_alert` runs a single Gemini
read-only diagnosis and posts it to Slack as **prose only** (no `approval_id`,
so no buttons). `handle_alert` derives an app name from the alert's `labels.pod`
(stripping the `-<rs>-<pod>` suffix, e.g. `shop-demo-54f67c8b9d-abcde` →
`shop-demo`) so `resolve_slack_webhook` can pick a per-app override, falling
back to the global `SLACK_WEBHOOK` env. Use it to confirm the Gemini + Slack
plumbing is wired:
```bash
curl -X POST "$NEXUS/webhook/alertmanager" \
  -H "Content-Type: application/json" \
  -d '{
    "receiver": "nexus-webhook", "status": "firing",
    "alerts": [{
      "status": "firing",
      "labels": {"alertname": "HighErrorRate", "severity": "critical",
                 "namespace": "default", "pod": "shop-demo-54f67c8b9d-abcde"},
      "annotations": {"description": "Error rate exceeds 5% for shop-demo"}
    }]
  }'

kubectl logs -n nexus deployment/nexus-api -f | grep -i "llm\|diagnosis\|chatops\|slack"
```

#### 9d. The interactive Approve/Reject path
Clickable buttons do **not** come from the alertmanager path. They come from the
**governed** staging path: when an incident cluster's LLM RCA proposes a
remediation, `NexusOrchestrator._stage_llm_remediation` →
`HumanApprovalQueue.enqueue()` stages the action AND publishes
`nexus.approvals.required` on NATS (carrying `app=<namespace>`) →
`Notifier.notify_approval_required` resolves the webhook via the same
`SLACK_WEBHOOK` fallback and posts the interactive message
(Approve ✅ / Reject ❌, `value=<approval_id>`).
- **Approve** click → `/slack/interactive` verifies the `v0` HMAC signature →
  calls `queue.approve(id)` → re-dispatches through `RunbookExecutor.execute_approved`
  (governance gates still apply) → Slack replaces the message with the outcome.
- **Reject** click → verifies signature → `queue.reject(id)` → workflow
  terminates, no cluster change.

The audit records Slack decisions as `triggered_by=human:slack:<user>`,
distinguishable from HTTP (`api_user`) and CLI approvals.

> **Single webhook var.** Both paths (governed notifier + chatops) resolve
> through `resolve_slack_webhook(app)`: a per-app `selfheal.yaml` override wins,
> else the global `SLACK_WEBHOOK` env fallback. One var, works without a mounted
> selfheal.yaml — the previous `SLACK_WEBHOOK_URL` is gone.

#### 9e. RBAC — let approved heals actually run
`restart_pod` (the L1 heal) deletes the target pod so its ReplicaSet recreates
it. The `nexus-api` ServiceAccount's ClusterRole must grant `delete` on pods —
without it an approved heal 403s:
```
pods "nexus-api" is forbidden: User "system:serviceaccount:nexus:nexus-api"
cannot delete resource "pods" in API group "" in the namespace "nexus"
```
That verb ships in `deploy/nexus/nexus-api-deployment.yaml` (ClusterRole
`nexus-api`, a dedicated `pods/delete` rule). If you already applied an older
copy, re-apply and rollout:
```bash
kubectl apply -f deploy/nexus/nexus-api-deployment.yaml
kubectl rollout restart deployment/nexus-api -n nexus
```
> Note: NEXUS can detect and try to heal its **own** degraded pod (`target=nexus/nexus-api`).
> That's a known self-referential case, not a misconfiguration — approving it
> restarts the API pod, which is safe because the pod is stateless and the heal
> is short-lived.

#### ⚡ Fast Verification — simulate a Slack click (no real incident needed)
To validate the new signature gate + form-body parsing + routing end-to-end,
POST a genuine `application/x-www-form-urlencoded` `payload` with a computed
`v0` signature straight at the callback (replace the Ngrok URL / approval id):
```bash
python - <<'EOF'
import hashlib, time, json, urllib.request, urllib.parse

SECRET = "your_slack_signing_secret"
NGROK  = "subdomain.ngrok-free.app"          # host only
APPROVAL_ID = "DEAD0001"                     # use one from: curl $NEXUS/approvals/pending

inner = json.dumps({"type": "block_actions",
        "user": {"id": "U123", "username": "vatsal"},
        "actions": [{"action_id": "nexus_reject", "value": APPROVAL_ID}]})
body = urllib.parse.urlencode({"payload": inner}).encode()
ts = str(int(time.time()))
sig = "v0=" + hashlib.sha256(b"v0:" + ts.encode() + b":" + body).hexdigest()

req = urllib.request.Request(
    f"https://{NGROK}/slack/interactive", data=body, method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded",
             "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
print(urllib.request.urlopen(req).read().decode())
EOF
#  ✅ 401 if SLACK_SIGNING_SECRET is wrong/missing  →  signature gate works
#  ✅ {"replace_original": true, "text": "❌ Rejected `DEAD0001` by @vatsal."}
```
A rejected/dispatched decision shows up in the audit:
```bash
curl -s $NEXUS/audit/tail?n=5 | python -m json.tool | grep -i "slack\|approval\|reject"
```
The HTTP/CLI equivalents hit the same queue+governance (useful when Slack isn't
configured): `curl -XPOST $NEXUS/approve/<id>`, `curl -XPOST $NEXUS/reject/<id>`,
`nexus approve <id>`, `nexus reject <id>`.

---

### 🐛 Common Issues

| Issue | Fix |
|---|---|
| Prescaler events all skipped | Lower gates: `kubectl set env deploy/nexus-api -n nexus NEXUS_PRESCALE_MIN_CONFIDENCE=0.01 NEXUS_PRESCALE_THRESHOLD_PCT=1` |
| NATS not reachable | Check `kubectl exec deployment/ppa-operator -- env \| grep NATS_URL` → must be `nats://nats.nexus.svc.cluster.local:4222` |
| PodMonitor not picked up | Ensure it has label `release: prometheus` in `monitoring` namespace |
| No metrics on port 9091 | Check `kubectl logs -l app=shop-demo \| grep "Prometheus metrics server"` |

---

**TL;DR order:** Phase 1 (NEXUS K8s) → Phase 2 (minikube+helm+shop-demo) → Phase 3 (apply PredictiveAutoscaler CRs) → Phase 4 (`ppa model push`) → Phase 5 (build + deploy operator) → verify logs. Use the **synthetic inject script** in the Fast Verification step for instant testing, and run **Phase 9** to test the Gemini LLM diagnosis path + the interactive Slack **Approve/Reject** approval workflow (setup details in [nexus_slack_approval_workflow.md](nexus_slack_approval_workflow.md)).