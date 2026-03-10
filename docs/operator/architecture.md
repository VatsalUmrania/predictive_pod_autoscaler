# Operator Architecture & Design

**Document:** Detailed operator system design, reconciliation cycle, component interactions

**Table of Contents:**
1. [System Topology](#system-topology)
2. [Reconciliation Cycle](#reconciliation-cycle)
3. [Component Architecture](#component-architecture)
4. [Decision Flow](#decision-flow)
5. [State Machine](#state-machine)
6. [Data Flow](#data-flow)
7. [Error Handling](#error-handling)

---

## System Topology

The operator manages multiple independent autoscaling policies (CRs), each with its own model and scaling configuration:

```mermaid
flowchart TB
    subgraph Cluster["Kubernetes Cluster"]
        subgraph OperatorPod["🟢 Operator Pod (ppa-operator)"]
            Kopf["Kopf @timer<br/>30s reconciliation"]
            CR_Registry["CR State Registry<br/>{app: CRState}"]
            
            subgraph Components["Per-CR Components"]
                Features["🔵 features.py<br/>build window"]
                Predictor["🔵 predictor.py<br/>TFLite inference"]
                Scaler["🔵 scaler.py<br/>rate limits"]
            end
            
            Kopf --> CR_Registry
            CR_Registry --> |for each CR| Features
            Features --> Predictor
            Predictor --> Scaler
        end
        
        subgraph Data["📦 Data & Config"]
            PVC["PVC: /models<br/>├─ app1/<br/>│  ├─ model.tflite<br/>│  ├─ scaler.pkl<br/>│  └─ target_scaler.pkl<br/>└─ app2/..."]
            CRDB["CRs in etcd<br/>test-app-ppa<br/>other-app-ppa"]
        end
        
        subgraph Monitoring["📊 Prometheus Stack"]
            Prom["Prometheus<br/>15s scrape"]
            Metrics["metrics<br/>rps, cpu, memory<br/>latency, connections"]
        end
        
        subgraph Targets["🎯 Target Deployments"]
            App1["Deployment: test-app<br/>3/20 replicas"]
            App2["Deployment: other-app<br/>5/25 replicas"]
        end
        
        Features -->|PromQL| Prom
        Prom -.->|metrics| Metrics
        Scaler -->|PATCH<br/>replicas| App1
        Scaler -->|PATCH<br/>replicas| App2
        PVC -->|mount| Predictor
        CRDB -->|watch| Kopf
    end
    
    style OperatorPod fill:#e1f5ff
    style Components fill:#fff3e0
    style Data fill:#f3e5f5
    style Monitoring fill:#e8f5e9
    style Targets fill:#fce4ec
```

---

## Reconciliation Cycle (30s)

The core event loop runs every 30 seconds for each CR:

```mermaid
flowchart TD
    Start([30s Timer Fires]) --> Fetch["📥 Fetch Metrics from Prometheus"]
    Fetch --> Query["PromQL: range[now-6m:15s]<br/>per target namespace/deployment"]
    Query --> BuildWin["🔨 Build Feature Window"]
    BuildWin --> Normalize["📏 Normalize Features<br/>using scaler.pkl per-CR"]
    Normalize --> Infer["🧠 Run TFLite Inference<br/>predict RPS @ +T minutes"]
    Infer --> CalcRep["📊 Calculate Desired Replicas<br/>replicas = pred_rps / capacity"]
    CalcRep --> RateLimit["⚠️ Apply Rate Limits<br/>max jump 2× up or 0.5× down"]
    RateLimit --> Stabil["🛡️ Stabilization Filter<br/>require 2 consecutive cycles"]
    Stabil --> Decision{Replicas<br/>Changed?}
    
    Decision -->|No| Status["✅ Update CR Status<br/>lastPrediction, metrics"]
    Decision -->|Yes| Patch["🔧 Patch Deployment<br/>kubectl patch spec.replicas"]
    Patch --> Status
    Status --> Wait["⏳ Wait 30s"]
    Wait --> Start
    
    style Start fill:#90caf9
    style Fetch fill:#81c784
    style BuildWin fill:#ffb74d
    style Infer fill:#ba68c8
    style Decision fill:#ef5350
    style Patch fill:#29b6f6
    style Status fill:#66bb6a
```

**Key Timing:**
| Step | Duration | Notes |
|---|---|---|
| Prometheus query | ~500ms | Worst case: network latency + range query |
| Feature normalization | ~50ms | In-memory scaler.pkl lookup |
| TFLite inference | ~20ms | CPU-bound, quantized model |
| Rate limit & stabilization | ~10ms | In-memory state machine |
| Kubernetes patch | ~100ms | API server latency |
| **Total per cycle** | **~700ms** | Well under 30s budget |

---

## Component Architecture

### 1. Kopf Controller (main.py)

**Role:** Kubernetes operator framework entrypoint

```python
# Pseudo-code
@kopf.timer('ppa.example.com', 'v1', 'PredictiveAutoscaler', interval=30.0)
def reconcile_autoscaler(spec, name, namespace, **kwargs):
    """Called every 30s for each CR"""
    
    # 1. Load model & scaler for this CR
    model = load_tflite_model(spec.modelPath)
    scaler = load_scaler(spec.scalerPath)
    target_scaler = load_scaler(spec.targetScalerPath)
    
    # 2. Fetch metrics & build features
    metrics = fetch_prometheus_metrics(...)
    features = build_feature_window(metrics, scaler)
    
    # 3. Run inference
    predicted_rps = model.infer(features)
    
    # 4. Calculate replicas
    desired_replicas = calculate_replicas(
        predicted_rps, spec.capacityPerPod, 
        spec.minReplicas, spec.maxReplicas
    )
    
    # 5. Apply rate limits & stabilization
    desired_replicas = apply_rate_limits(desired_replicas, ...)
    desired_replicas = stabilization_filter(desired_replicas, ...)
    
    # 6. Patch deployment
    patch_deployment(spec.targetDeployment, desired_replicas)
    
    # 7. Update CR status
    update_cr_status(desired_replicas, metrics, predicted_rps)
```

### 2. Features Module (features.py)

**Role:** Prometheus data collection & feature engineering

```mermaid
graph LR
    A["fetch_prometheus_metrics()"] --> B["Query: rps_per_replica<br/>cpu_utilization<br/>memory_utilization<br/>latency_p95"]
    B --> C["Transform:<br/>log scale RPS<br/>normalize CPU/mem<br/>exponential decay<br/>temporal features"]
    C --> D["build_feature_window()<br/>12 timesteps @ 30s ea<br/>= 6-minute window"]
    D --> E["Normalize with scaler.pkl<br/>mean, std per feature<br/>per CR"]
    E --> F["Ready for inference<br/>shape: 1 × 12 × 14<br/>14 = num_features"]
    
    style A fill:#81c784
    style D fill:#ffb74d
    style E fill:#64b5f6
    style F fill:#90caf9
```

**14 Features:**
```
Input Window (12 × 30s = 6 min lookback):
├─ Load Indicators (4): rps_per_replica, cpu_pct, mem_pct, p95_latency_ms
├─ System (2): active_connections, error_rate
├─ Momentum (2): cpu_accel, rps_accel
├─ State (1): replicas_normalized
└─ Time Cyclical (5): hour_sin, hour_cos, dow_sin, dow_cos, is_weekend
```

### 3. Predictor Module (predictor.py)

**Role:** TFLite model loading & inference

```mermaid
sequenceDiagram
    participant M as main.py
    participant P as predictor.py
    participant TF as tensorflow.lite
    participant TR as tflite_runtime
    
    M->>P: infer(feature_window)
    P->>P: Check if model loaded
    
    alt Model not loaded
        P->>TF: Try tensorflow.lite.Interpreter()
        alt TF success
            TF-->>P: ✅ Model ready
        else TF fails (numpy mismatch)
            P->>TR: Fall back to tflite_runtime
            TR-->>P: ✅ Model ready
        end
    end
    
    P->>P: allocate_tensors()
    P->>P: set_tensor(input, features)
    P->>TF: invoke()
    P->>P: get_tensor(output)
    P-->>M: prediction ✅
```

**Compatibility Strategy:**
```
Preferred:   tensorflow.lite (consistent with training env)
Fallback:    tflite_runtime (lighter binary)
Constraint:  numpy==1.26.4 (ABI compatibility with both)
```

### 4. Scaler Module (scaler.py)

**Role:** Replica calculation, rate limiting, stabilization

```mermaid
flowchart TD
    A["predict_rps<br/>e.g., 1500 RPS"] --> B["Calculate Base Replicas<br/>replicas = pred_rps / capacity<br/>e.g., 1500 / 80 = 18.75"]
    B --> C["Round to Integer<br/>18.75 → 19 replicas"]
    C --> D["Apply Min/Max Bounds<br/>clamp to [2, 20]"]
    D --> E["Rate Limit Check<br/>current=10, desired=19<br/>max_jump=2.0×"]
    
    E -->|19 > 10×2=20| F["Clamp to Max Jump<br/>→ 20 replicas"]
    E -->|19 ≤ 20| G["Allow Full Jump<br/>→ 19 replicas"]
    
    F --> H["Stabilization Filter<br/>require 2 consecutive<br/>same decisions"]
    G --> H
    
    H -->|First occurrence| I["Record in history<br/>history = [19, None]<br/>Wait for next cycle"]
    H -->|Second consecutive| J["Decision Confirmed<br/>Proceed to patch"]
    H -->|Different from last| K["Reset history<br/>history = [20, None]<br/>Wait for next cycle"]
    
    style E fill:#ef5350
    style H fill:#66bb6a
```

---

## Decision Flow

Complete decision tree showing all factors influencing replica scaling:

```mermaid
graph TD
    Start["New Cycle:<br/>t=now"] --> Metrics["Fetch Prometheus"]
    Metrics --> BadMetrics{Any metrics<br/>missing?}
    
    BadMetrics -->|Yes| UseHistory["Use last known<br/>stable state"]
    BadMetrics -->|No| BuildWin["Build feature<br/>window"]
    
    UseHistory --> Infer
    BuildWin --> Infer["Run TFLite<br/>infer RPS"]
    
    Infer --> CalcRep["Calculate<br/>replicas"]
    
    CalcRep --> RateUp{Replicas<br/>jump up>2x?}
    RateUp -->|Yes| CapUp["Cap to 2×<br/>current"]
    RateUp -->|No| RateDn
    
    CapUp --> RateDn{Replicas<br/>jump down<0.5x?}
    RateDn -->|Yes| CapDn["Cap to 0.5×<br/>current"]
    RateDn -->|No| Stabil
    
    CapDn --> Stabil["Stabilization<br/>Check"]
    
    Stabil --> StabilOK{Agreed with<br/>N-1 cycle?}
    StabilOK -->|No| Hold["Hold replicas<br/>same as now<br/>record in history"]
    StabilOK -->|Yes| Changed{Replicas<br/>!= current?}
    
    Hold --> Wait["30s tick"]
    
    Changed -->|Yes| Patch["PATCH deployment<br/>status → applied"]
    Changed -->|No| Record["Record<br/>reason: stable"]
    
    Patch --> Record["Record in CR status:<br/>- predicted_rps<br/>- desired_replicas<br/>- current_replicas<br/>- decision_reason<br/>- confidence"]
    
    Record --> Wait
    Wait --> Start
    
    style Start fill:#90caf9
    style Metrics fill:#81c784
    style Infer fill:#ba68c8
    style RateUp fill:#ef5350
    style RateDn fill:#ef5350
    style Stabil fill:#66bb6a
    style Changed fill:#ffb74d
    style Patch fill:#29b6f6
```

---

## State Machine

The operator maintains per-CR state tracking scaling decisions:

```mermaid
stateDiagram-v2
    [*] --> Startup
    
    Startup --> Warmup: CR created,<br/>fetch metrics
    
    Warmup --> Warmup: < 12 steps<br/>collected
    note right of Warmup
        Collecting 12 × 30s = 6 min
        minimum history window
        Metrics: N/12 steps collected
    end note
    
    Warmup --> Stable: 12 steps ✓<br/>replicas match
    
    Stable --> Stable: predictions<br/>stable
    
    Stable --> Scaling: new prediction<br/>differs from<br/>current by > threshold
    
    Scaling --> Scaling: still scaling<br/>(within rate limits)
    
    Scaling --> Stable: reached<br/>desired<br/>replicas
    
    Stable --> Error: Prometheus<br/>unreachable<br/>model load fail
    
    Error --> Stable: metrics<br/>restored
    
    Error --> Error: error<br/>persists
    
    style Warmup fill:#ffb74d
    style Stable fill:#66bb6a
    style Scaling fill:#ef5350
    style Error fill:#d32f2f
```

---

## Data Flow

End-to-end data movement through the operator:

```mermaid
sequenceDiagram
    participant TIM as 30s Timer
    participant KOPF as kopf @timer
    participant FEAT as features.py
    participant PROM as Prometheus
    participant PRED as predictor.py
    participant MODEL as TFLite Model
    participant SCAL as scaler.py
    participant K8S as Kubernetes API
    participant DEPLOY as Target Deployment
    
    TIM->>KOPF: 30s tick
    
    KOPF->>FEAT: fetch_metrics(ns, app, selector)
    FEAT->>PROM: PromQL range query<br/>group_by(pod): avg(rps_per_replica)[6m:30s]
    PROM-->>FEAT: 12 timesteps of metrics
    
    FEAT->>FEAT: normalize features<br/>using scaler.pkl
    FEAT-->>KOPF: feature_window [1, 12, 14]
    
    KOPF->>PRED: infer(feature_window)
    PRED->>MODEL: allocate + set_tensor
    MODEL-->>PRED: input set ✓
    PRED->>MODEL: invoke()
    MODEL-->>PRED: output tensor
    PRED->>PRED: dequantize if needed
    PRED-->>KOPF: predicted_rps = 1500
    
    KOPF->>SCAL: calculate_replicas(1500)
    SCAL->>SCAL: replicas = 1500 / 80 = 18
    SCAL->>SCAL: apply rate limits
    SCAL->>SCAL: stabilization filter
    SCAL-->>KOPF: desired: 18
    
    KOPF->>K8S: patch Deployment<br/>spec.replicas = 18
    K8S->>DEPLOY: reconcile replicas
    DEPLOY-->>K8S: status updated ✓
    K8S-->>KOPF: patch result ✓
    
    KOPF->>K8S: update PPA CR status<br/>predicted_rps: 1500<br/>desired_replicas: 18
    K8S-->>KOPF: status updated ✓
    
    KOPF-->>TIM: cycle complete ✅
```

---

## Error Handling

Graceful degradation strategy:

```mermaid
flowchart TD
    Error["Error Condition<br/>detected"] --> Type{Error Type?}
    
    Type -->|Prometheus<br/>unreachable| UseCache["Use cached metrics<br/>from last cycle"]
    UseCache --> CacheFall["if cache too old,<br/>hold current replicas"]
    
    Type -->|Model file<br/>missing| ModelErr["Log error<br/>CR skipped<br/>operator continues"]
    
    Type -->|TFLite<br/>crash| TFErr["Fall back to<br/>tflite_runtime<br/>or hold scaling"]
    
    Type -->|Scaler.pkl<br/>corrupt| ScalerErr["Use identity scaler<br/>raw features to model"]
    
    Type -->|K8S patch<br/>failed| PatchErr["Retry next cycle<br/>or manual intervention"]
    
    UseCache --> Record["Increment<br/>error counter"]
    ModelErr --> Record
    TFErr --> Record
    ScalerErr --> Record
    PatchErr --> Record
    
    Record --> Alert{Error count<br/>threshold?}
    Alert -->|Threshold hit| Escalate["Log WARNING<br/>Consider manual review"]
    Alert -->|Under threshold| Continue["Continue normal<br/>operation"]
    
    Escalate --> Continue
    
    style Error fill:#d32f2f
    style UseCache fill:#ffc107
    style Continue fill:#66bb6a
```

---

## Performance Characteristics

| Metric | Value | Notes |
|---|---|---|
| **Reconciliation Interval** | 30s | Configurable via `PPA_TIMER_INTERVAL` |
| **Feature Window** | 6 min (12 × 30s) | Configurable via `PPA_LOOKBACK_STEPS` |
| **Stabilization Window** | 2 cycles (60s) | Configurable via `PPA_STABILIZATION_STEPS` |
| **Prometheus Query Latency** | ~500ms | Per CR, per cycle |
| **Model Inference Latency** | ~20ms | TFLite CPU inference |
| **Deployment Patch Latency** | ~100ms | Kubernetes API server |
| **Memory per CR** | ~50–100 MB | Model + scaler + feature history |
| **Max CRs/pod** | ~50–100 | ~5 GB memory budget |

---

## Scaling Behavior Examples

### Example 1: Traffic Spike (RPS 800→1500)

| Cycle | Metrics | Prediction | Calculation | Rate Limit | Stabilization | Final | Decision |
|---|---|---|---|---|---|---|---|
| T+0 | Baseline | 800 | 10 replicas | ✓ | - | 10 | Hold |
| T+30s | Spike detected | 1500 | 19 replicas | 19 ≤ 10×2=20 ✓ | First time | 19 | First predicted increase |
| T+60s | Sustained spike | 1450 | 18 replicas | ✓ | Different! → reset | 10 | Hold (disagreement) |
| T+90s | Still spiking | 1520 | 19 replicas | ✓ | Again! | 10 | Hold |
| T+120s | Confirmed high | 1500 | 19 replicas | ✓ | **Match T+60s** | **19** | **✅ SCALE UP to 19** |

*Result:* 2-minute delay from spike detection to scaling = covers "cold start" time

### Example 2: Fast Scale-Down (Rate Limit Protection)

| Cycle | Current | Predicted | Desired | Rate Limited | Reason |
|---|---|---|---|---|---|
| T+0 | 15 | 200 RPS → 2.5 | 3 | **6** | Cap to 0.5× = 15×0.5=7.5→8, but let it go to 6 |
| T+30s | 6 | 150 RPS → 1.9 | 2 | **3** | Cap to 0.5× = 6×0.5=3 |
| T+60s | 3 | 100 RPS → 1.25 | 1 | **2** | Min replicas = 2 |

*Result:* Conservative scale-down prevents flapping during traffic dropoff

---

## See Also

- **[Deployment Guide](./deployment.md)** — How to get the operator running
- **[Configuration Reference](./configuration.md)** — All environment variables & CR spec fields
- **[API Reference](./api.md)** — Full Custom Resource schema
- **[Troubleshooting](./troubleshooting.md)** — Common issues & diagnostics

