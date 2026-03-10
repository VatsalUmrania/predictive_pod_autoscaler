# Operator API Reference

**Custom Resource Definition (CRD) schema, validation rules, and complete YAML examples**

---

## CRD Schema

### Full OpenAPI v3 Spec

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: predictiveautoscalers.ppa.example.com
spec:
  group: ppa.example.com
  scope: Namespaced
  names:
    kind: PredictiveAutoscaler
    plural: predictiveautoscalers
    singular: predictiveautoscaler
    shortNames:
      - ppa
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          required:
            - spec
          properties:
            metadata:
              type: object
              properties:
                name:
                  type: string
                  maxLength: 253
                  pattern: '^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$'
                namespace:
                  type: string
            spec:
              type: object
              required:
                - targetDeployment
                - namespace
                - modelPath
                - scalerPath
                - targetScalerPath
                - minReplicas
                - maxReplicas
                - capacityPerPod
                - scaleUpRate
                - scaleDownRate
              properties:
                # Target Deployment
                targetDeployment:
                  type: string
                  description: "Name of the Deployment to autoscale"
                  minLength: 1
                  maxLength: 253
                namespace:
                  type: string
                  description: "Namespace containing target deployment"
                  default: "default"
                containerName:
                  type: string
                  description: "Optional: specific container name (auto-detect if omitted)"
                  
                # Model & Scalers
                modelPath:
                  type: string
                  description: "Path to TFLite model in /models PVC"
                  minLength: 1
                  pattern: '.*\.tflite$'
                scalerPath:
                  type: string
                  description: "Path to input feature scaler (joblib pickle)"
                  minLength: 1
                  pattern: '.*\.pkl$'
                targetScalerPath:
                  type: string
                  description: "Path to target (RPS) scaler (joblib pickle)"
                  minLength: 1
                  pattern: '.*\.pkl$'
                  
                # Scaling Bounds
                minReplicas:
                  type: integer
                  description: "Minimum replicas (fail-safe lower bound)"
                  minimum: 1
                  maximum: 1000
                maxReplicas:
                  type: integer
                  description: "Maximum replicas (cost/resource upper bound)"
                  minimum: 1
                  maximum: 10000
                capacityPerPod:
                  type: number
                  description: "RPS per pod at 100% utilization"
                  exclusiveMinimum: 0
                  maximum: 10000
                  
                # Rate Limits
                scaleUpRate:
                  type: number
                  description: "Max multiplier per cycle (e.g., 2.0 = max 2× jump)"
                  minimum: 1.0
                  maximum: 10.0
                scaleDownRate:
                  type: number
                  description: "Min multiplier per cycle (e.g., 0.5 = min 50%)"
                  minimum: 0.1
                  maximum: 1.0
            status:
              type: object
              properties:
                consecutiveSkips:
                  type: integer
                  description: "Number of skipped reconciliation cycles"
                lastPrediction:
                  type: object
                  properties:
                    timestamp:
                      type: string
                      format: date-time
                    predictedRPS:
                      type: number
                    desiredReplicas:
                      type: integer
                    currentReplicas:
                      type: integer
                    reason:
                      type: string
                    confidence:
                      type: number
                      minimum: 0
                      maximum: 1
```

---

## Example CRs

### Minimal (Single-Page App)

```yaml
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: spa-ppa
  namespace: default
spec:
  targetDeployment: single-page-app
  namespace: default
  modelPath: /models/spa/ppa_model.tflite
  scalerPath: /models/spa/scaler.pkl
  targetScalerPath: /models/spa/target_scaler.pkl
  minReplicas: 1
  maxReplicas: 10
  capacityPerPod: 100
  scaleUpRate: 2.0
  scaleDownRate: 0.5
```

### Production API Server

```yaml
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: api-server-ppa
  namespace: production
spec:
  targetDeployment: api-server
  namespace: production
  containerName: api                    # Specific container
  modelPath: /models/api-server/ppa_model.tflite
  scalerPath: /models/api-server/scaler.pkl
  targetScalerPath: /models/api-server/target_scaler.pkl
  minReplicas: 5                        # Always keep 5 for high availability
  maxReplicas: 100                      # Allow massive scale-out
  capacityPerPod: 50                    # Conservative (high compute requirements)
  scaleUpRate: 3.0                      # Aggressive scale-up (latency critical)
  scaleDownRate: 0.3                    # Conservative scale-down (prevent flapping)
```

### Multi-Horizon (Choosing Best Model)

```yaml
# Use rps_t5m (5-minute horizon) as recommended default
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: web-tier-ppa
  namespace: default
spec:
  targetDeployment: web-tier
  namespace: default
  # 5-minute horizon best balances cold-start + responsiveness
  modelPath: /models/web-tier/rps_t5m/ppa_model.tflite
  scalerPath: /models/web-tier/rps_t5m/scaler.pkl
  targetScalerPath: /models/web-tier/rps_t5m/target_scaler.pkl
  minReplicas: 2
  maxReplicas: 50
  capacityPerPod: 80
  scaleUpRate: 2.0
  scaleDownRate: 0.5
```

---

## Creating/Managing CRs

### Create CR from YAML file

```bash
kubectl apply -f my-autoscaler.yaml
```

### Create CR inline

```bash
kubectl apply -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: my-app-ppa
spec:
  targetDeployment: my-app
  namespace: default
  modelPath: /models/my-app/ppa_model.tflite
  scalerPath: /models/my-app/scaler.pkl
  targetScalerPath: /models/my-app/target_scaler.pkl
  minReplicas: 2
  maxReplicas: 20
  capacityPerPod: 80
  scaleUpRate: 2.0
  scaleDownRate: 0.5
EOF
```

### Update CR

```bash
# Edit in default editor
kubectl edit ppa my-app-ppa

# Or patch specific field
kubectl patch ppa my-app-ppa -p '{"spec":{"maxReplicas":30}}'
```

### Delete CR

```bash
# Stops autoscaling but leaves deployment untouched
kubectl delete ppa my-app-ppa

# Delete all CRs in namespace
kubectl delete ppa --all
```

---

## Status Subresource

The operator updates CR status with detailed reconciliation information:

### Check status

```bash
kubectl get ppa my-app-ppa -o yaml | tail -20
```

### Status structure

```yaml
status:
  consecutiveSkips: 0               # Cycles skipped (0 = no errors)
  lastPrediction:
    timestamp: "2026-03-10T10:15:30Z"  # Last reconciliation time
    predictedRPS: 1250                 # Model output (RPS prediction)
    desiredReplicas: 16                # After all rate limiting
    currentReplicas: 16                # Actual replicas deployed
    reason: "Stable: in steady state"  # Decision reason
    confidence: 0.92                   # Prediction confidence (0–1)
```

### Interpreting status reasons

| Reason | Meaning | Action |
|---|---|---|
| `Stable: steady state` | Predictions stable, no scaling needed | Normal operation ✓ |
| `Warming up: N/24 steps` | Still collecting initial data | Wait (usually 12 min) |
| `Rate limited: 5→10` | Would scale 5, but limiter allows 10 | Normal (safety mechanism) |
| `Error: Prometheus unreachable` | Can't fetch metrics | Check Prometheus URL |
| `Error: Model not found` | `.tflite` file missing in PVC | Check modelPath |
| `Skipped: N consecutive cycles` | Repeated errors, CR paused | Debug and fix, restart |

---

## Validation Rules

The CRD enforces strict validation:

### Field Constraints

```yaml
# Valid
minReplicas: 2
maxReplicas: 50
# Invalid: minReplicas > maxReplicas
minReplicas: 50
maxReplicas: 2
# Error: spec.minReplicas must be less than max...

---

# Valid
capacityPerPod: 0.5    # Fractional OK
# Invalid: capacityPerPod: 0 or -1
# Error: must be > 0

---

# Valid
scaleUpRate: 2.5       # Between 1.0 and 10.0
# Invalid: scaleUpRate: 0.5
# Error: must be >= 1.0

---

# Valid
modelPath: /models/app/model.tflite
# Invalid: modelPath: /models/app/model.pb  (wrong format)
# Error: must end with .tflite
```

### Validation Errors

```bash
# Example: trying to create with invalid maxReplicas < minReplicas
kubectl apply -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: bad-ppa
spec:
  targetDeployment: my-app
  minReplicas: 20
  maxReplicas: 10  # Invalid!
  ...
EOF

# Output:
# error: error validating "a-ppa.yaml": error validating data: [spec.maxReplicas: Invalid value: 10: spec.maxReplicas must be greater than minReplicas]
```

---

## Kubernetes API Compatibility

### Kubectl shorthand

```bash
# These work due to shortNames: ["ppa"]
kubectl get ppa                    # ≡ kubectl get predictiveautoscaler
kubectl describe ppa my-app-ppa
kubectl edit ppa my-app-ppa
kubectl delete ppa my-app-ppa
```

### API group & version

```bash
# Full API reference:
# Group:   ppa.example.com
# Version: v1
# Kind:    PredictiveAutoscaler

# Query CRD info:
kubectl get crd predictiveautoscalers.ppa.example.com -o yaml
```

### RBAC Permissions

Required RBAC for operator service account:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ppa-operator
rules:
  # Read CRs
  - apiGroups: ["ppa.example.com"]
    resources: ["predictiveautoscalers"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["ppa.example.com"]
    resources: ["predictiveautoscalers/status"]
    verbs: ["patch", "update"]
  
  # Patch deployments
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "patch", "update"]
  
  # Read pods (for label queries)
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
```

---

## Common CR Pattern

### Multi-namespace setup

Deploy one operator pod, manage multiple apps across namespaces:

```bash
# App in namespace-1
kubectl apply -n namespace-1 -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: app1-ppa
  namespace: namespace-1
spec:
  targetDeployment: app1
  namespace: namespace-1
  ...
EOF

# App in namespace-2
kubectl apply -n namespace-2 -f - <<EOF
apiVersion: ppa.example.com/v1
kind: PredictiveAutoscaler
metadata:
  name: app2-ppa
  namespace: namespace-2
spec:
  targetDeployment: app2
  namespace: namespace-2
  ...
EOF

# Verify both
kubectl get ppa --all-namespaces
```

---

## See Also

- **[Configuration Reference](./configuration.md)** — Tuning guide & environment variables
- **[Commands](./commands.md)** — Useful kubectl commands for CR management
- **[Troubleshooting](./troubleshooting.md)** — Debug CR validation errors

