# PPA + NEXUS Predictive Plane Integration Design

**Date:** 2026-06-04
**Status:** Draft — pending implementation plan
**Owner:** Vatsal Umrania

---

## 1. Overview

Wire PPA's LSTM predictions into NEXUS as the **Predictive Plane**, so NEXUS is the sole
decision and governance authority and PPA is the prediction engine only.

### Design principle

```
PPA = Prediction Engine  (LSTM forecast)
NEXUS = Decision + Governance  (ActionLadder, OPA policies, cooldowns)
Kubernetes = Execution target  (scale Deployment)
```

**Primary goal:** NEXUS pre-scales proactively 10 minutes before traffic spikes hit,
using real LSTM forecasts from PPA instead of heuristics. Time-horizon separation
avoids conflict between the two systems.

### System data flow

```
PPA Operator (kopf timer, 30s)
  └─ predict()  → LSTM inference → raw predicted_rps
  └─ decide()   → stabilization / multi-model aggregation (in-memory only)
  └─ observe()  → log prediction to NATS ppa.predictions.{deployment}
                  (observer mode: no K8s scale patch from PPA)

NEXUS Server
  ├─ PreScaler subscribes ppa.predictions.*
  │     ActionLadder.evaluate()  → OPA policy, cooldown, circuit breaker
  │     if approved → K8s Deployment scale patch
  │     SHADOW / ADVISORY / AUTONOMOUS governed modes
  │
  ├─ RCA Engine  ← receives prediction context (14 features + confidence)
  │     boosts confidence scoring when prediction aligns with anomaly pattern
  │
  └─ PPA Outcome Tracker
        after horizon_minutes → compare predicted_rps vs actual_rps
        publishes verdict to ppa.outcomes.{deployment}
        nightly batch writes /data/ppa_outcomes.jsonl for PPA retraining
```

### New NATS subjects

| Subject | Direction | Purpose |
|---|---|---|
| `ppa.predictions.{deployment}` | PPA → NEXUS | LSTM prediction event, 30s cadence |
| `ppa.outcomes.{deployment}` | NEXUS → PPA | Spike outcome verdict, per horizon |

---

## 2. Changes by Component

### 2.1 PPA — New event type

**File:** `src/ppa/bus/prediction_event.py` (new)

```python
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class PpaPredictionEvent:
    deployment:       str
    namespace:        str
    predicted_rps:    float      # LSTM raw output (before safety factor)
    current_rps:      float      # Observed RPS at prediction time
    confidence:       float      # [0, 1] from predictor metadata / concept drift score
    horizon_minutes:  int        # Lookback horizon = 10 (minutes)
    model_version:    str
    raw_features:     dict[str, float]   # All 14 Prometheus features (for RCA)
    timestamp:        str        # ISO UTC

    def to_dict(self) -> dict:
        ...

    def to_nats_payload(self) -> bytes:
        ...
```

### 2.2 PPA — NATSClient integration (ScalerStateMachine)

**File:** `src/ppa/operator/state_machine.py`

Add `NATSClient` to `ScalerStateMachine` (`__init__`):

```python
from nexus.bus.nats_client import NATSClient
import asyncio

class ScalerStateMachine:
    def __init__(self, ...):
        self._nats = NATSClient()   # lazy connect on first publish
        self._pending_events: deque = deque(maxlen=100)
        # Flush on reconnect
```

Add new method after `predict()`:

```python
    async def _publish_prediction(self, predicted_rps: float, features: dict) -> None:
        event = PpaPredictionEvent(
            deployment      = self.config["target"],
            namespace       = self.config["target_ns"],
            predicted_rps   = predicted_rps,
            current_rps     = features.get("requests_per_second", 0.0),
            confidence      = self._compute_confidence(),
            horizon_minutes = self.config["target_horizon"],
            model_version   = self.state.predictor.version or "unknown",
            raw_features    = features,
            timestamp       = datetime.now(timezone.utc).isoformat(),
        )
        subject = f"ppa.predictions.{self.config['target']}"
        try:
            await self._nats.publish(subject, event.to_dict())
        except Exception:
            self._pending_events.append(event)   # accumulate, flush on reconnect
```

Replace `act()` scaling logic with observer-mode publish:

```python
    def act(self, desired: int, current: int) -> None:
        if self.config["observer_mode"]:
            # Observer mode: log what PPA would do, publish prediction for NEXUS
            logger.info(
                f"[{self.cr_name}] OBSERVER: would scale {current}→{desired}, "
                f"publishing to NATS for NEXUS governance"
            )
            # Publish to NATS asynchronously (safe without blocking kopf)
            asyncio.get_event_loop().create_task(
                self._publish_prediction(self.state.last_prediction, ...)
            )
        else:
            # Legacy: PPA owns scaling decisions directly
            self._legacy_act(desired, current)
```

**Async bridging from kopf sync context:**
Use `asyncio.get_event_loop().create_task(...)` — kopf runs in an async context
when using `@kopf.timer`. The event loop is already live; `create_task` schedules
the coroutine without blocking the reconciliation thread.

### 2.3 NEXUS — Prescaler subscription to PPA predictions

**File:** `src/nexus/predictive/prescaler.py`

Add a new `subscribe_to_ppa()` method:

```python
async def subscribe_to_ppa_predictions(self) -> None:
    """Subscribe to ppa.predictions.* — replaces DBTrafficCorrelator as primary driver."""
    async def handler(data: dict, subject: str) -> None:
        if not subject.startswith("ppa.predictions."):
            return
        deployment = subject.split(".")[-1]
        # Route to handle_spike_prediction compatible shape
        event = IncidentEvent(
            agent       = AgentType.ORCHESTRATOR,
            signal_type = SignalType.TRAFFIC_SPIKE_PREDICTED,
            severity    = Severity.WARNING,
            namespace   = data.get("namespace"),
            resource_name = deployment,
            context = {
                "current_rps":             data["current_rps"],
                "predicted_rps":           data["predicted_rps"],
                "confidence":              data["confidence"],
                "endpoint":                f"/{deployment}",
                "prediction_horizon_minutes": data["horizon_minutes"],
                "db_table_trigger":        None,
                "ppa_model_version":       data["model_version"],
                "raw_features":            data["raw_features"],
            },
            confidence = data["confidence"],
        )
        await self.handle_spike_prediction(event)

    await self._nats.subscribe("ppa.predictions.*", handler=handler)
```

Update fallback hierarchy comment:

```python
    # Prediction source priority:
    # 1. ppa.predictions.{deployment}   ← primary (LSTM, 14 features)
    # 2. DBTrafficCorrelator            ← fallback (heuristic)
    # 3. EWMATrafficModel               ← last resort (no external deps)
```

### 2.4 NEXUS — Outcome Tracker (new)

**File:** `src/nexus/learning/ppa_outcome_tracker.py` (new)

```python
class PpaOutcomeTracker:
    """
    Closes the feedback loop between NEXUS PreScaler and PPA model retraining.

    1. Subscribes to ppa.predictions.{deployment} — stores predicted_rps + horizon
    2. Monitors actual RPS after horizon_minutes elapses
    3. Publishes verdict to ppa.outcomes.{deployment}
    4. Nightly: writes /data/ppa_outcomes.jsonl for PPA training pipeline
    """

    def __init__(
        self,
        nats_client: NATSClient,
        outcome_db_path: Path | None = None,
        batch_output_path: Path | None = None,
    ):
        self._nats      = nats_client
        self._pending:  dict[str, PendingPrediction] = {}   # decision_id → prediction
        self._batch_out = batch_output_path or Path("data/ppa_outcomes.jsonl")

    async def on_prediction(self, event: PpaPredictionEvent) -> None:
        """Called when a ppa.predictions.* event arrives — store for later verdict."""
        ...

    async def _check_and_emit_outcome(self) -> None:
        """Poll pending predictions; emit outcome when horizon has elapsed."""
        now = datetime.now(timezone.utc)
        for decision_id, pred in list(self._pending.items()):
            if now >= pred.expected_resolution_time:
                actual_rps = await self._fetch_actual_rps(pred.deployment)
                verdict    = self._compute_verdict(pred, actual_rps)
                outcome_event = {
                    "deployment":    pred.deployment,
                    "namespace":     pred.namespace,
                    "predicted_rps": pred.predicted_rps,
                    "actual_rps":    actual_rps,
                    "verdict":       verdict,    # "spike_hit" | "spike_missed" | "correct_no_spike"
                    "smape":         self._compute_smape(pred.predicted_rps, actual_rps),
                    "confidence":    pred.confidence,
                    "model_version": pred.model_version,
                    "resolution_at":  now.isoformat(),
                }
                await self._nats.publish(
                    f"ppa.outcomes.{pred.deployment}", outcome_event
                )
                del self._pending[decision_id]

    async def run_batch_flush(self) -> None:
        """Called nightly: write all recent outcomes to JSONL for PPA retraining."""
        ...
```

### 2.5 NEXUS OutcomeTracker — live PPA feedback

**File:** `src/nexus/learning/feedback_loop.py`

Extend `FeedbackLoop._store_signal_patterns()` to also write PPA outcomes to the
 OutcomeStore audit trail so they're visible in NEXUS dashboards:

```python
async def _store_ppa_outcome(self, outcome: dict) -> None:
    """Record PPA prediction accuracy in OutcomeStore for analytics."""
    await self._store.write_ppa_outcome(outcome)
```

### 2.6 New CRD field

**File:** `deploy/crd-predictiveautoscaler.yaml`

Add `observerMode` field (boolean, default `false`):

```yaml
spec:
  observerMode:
    type: boolean
    default: false
    description: >
      When true, PPA emits predictions to NATS and defers all scaling
      decisions to NEXUS Prescaler. When false (default), PPA retains
      native scaling authority for backward compatibility.
```

---

## 3. Governance Contract

- All K8s scale actions for PredictiveAutoscaler-managed deployments flow through
  `ActionLadder.evaluate()` — no direct `scale_deployment()` call from PPA in observer mode.
- `GovernanceCircuitBreaker` applies to PreScaler AUTONOMOUS-mode scaling actions.
- `HumanApprovalQueue` stages ADVISORY-mode actions requiring human review.
- PPA observer-mode reconciliation logs: predicted replicas, current replicas, NEXUS
  subject published — provides full audit trail of prediction vs. NEXUS decision.
- `NEXUS_PRESCALE_COOLDOWN_S` applies per-deployment; PPA stabilization logic (`STABILIZATION_STEPS`)
  is superseded — NEXUS cooldown store is authoritative in observer mode.

---

## 4. Error Handling

### NATS connectivity failure in PPA

- If NATS publish fails: append event to `self._pending_events` deque (max 100).
- On next successful publish: flush queued events in order.
- Log warning on first failure; error on deque overflow.
- **PPA reconciliation continues** — never block kopf timer on NATS IO.

### NEXUS PreScaler degraded mode

- If `ppa.predictions.*` subscription raises connection error:
  PreScaler falls back to `DBTrafficCorrelator` as primary driver.
- Set `prescaler_degraded = True`; emit `prescaler_source_fallback` metric.
- Log: `PRE_SCALER_DEGRADED: falling back to DBTrafficCorrelator`.

### NEXUS unavailable

- PPA in observer mode: **no scaling occurs** (fail-closed safe state).
- Operator can restore native PPA scaling by setting `observerMode: false`.
- When NEXUS recovers: PreScaler re-subscribes; observer mode resumes automatically.

### K8s API failure during NEXUS scale

- `ActionLadder.evaluate()` returns `outcome = "scale_failed"`.
- `GovernanceCircuitBreaker` increments failure count.
- `OutcomeStore` records `execution_outcome = "failed"` — visible in NEXUS dashboards.

---

## 5. Migration Path

### Phase 1: Infrastructure (this spec)

1. Create `src/ppa/bus/prediction_event.py` — `PpaPredictionEvent` dataclass.
2. Add `NATSClient` to `ScalerStateMachine`; implement `_publish_prediction()`.
3. Convert `act()` to observer-mode branch (log + publish, no K8s patch).
4. Add `src/nexus/learning/ppa_outcome_tracker.py` — outcome tracking + batch writer.
5. Update Prescaler to subscribe to `ppa.predictions.*` as primary source.
6. Update CRD YAML with `observerMode` field.

### Phase 2: Wiring

7. Connect PPA OutcomeTracker verdict → `ppa.outcomes.*` NATS subject.
8. Connect nightly batch flush → `/data/ppa_outcomes.jsonl`.
9. Wire `raw_features` from PPA event into NEXUS RCA confidence scoring.

### Phase 3: Rollout

10. **Shadow mode first**: deploy PPA operator with `NATSClient` enabled but `observerMode: false`.
    Verify prediction events flow to NEXUS without affecting existing scaling.
11. **One CR at a time**: set `observerMode: true` on one PredictiveAutoscaler CR.
    Verify NEXUS PreScaler receives events, ActionLadder evaluates, K8s scale succeeds.
12. **Autonomous promotion**: after 20 shadow predictions with precision >= 0.70,
    promote `NEXUS_PRESCALE_MODE` from `shadow` → `advisory` → `autonomous`.

### Rollback

- Set `observerMode: false` on CR → PPA operator resumes direct scaling.
- `NEXUS_PRESCALE_MODE=shadow` keeps PreScaler log-only without affecting scaling state.

---

## 6. Testing Strategy

| Test | Scope | Key assertions |
|---|---|---|
| `PpaPredictionEvent.to_dict()` | Unit | All fields present, confidence in [0,1], valid timestamp |
| `ScalerStateMachine._publish_prediction()` with mock NATS | Unit | Correct subject, correct payload shape |
| `ScalerStateMachine.act()` observer mode | Unit | No `scale_deployment` call; `_publish_prediction` called |
| Prescaler subscribes to `ppa.predictions.*` and routes to ActionLadder | Integration | `handle_spike_prediction` called with correct context |
| PPA outcome verdict → `ppa.outcomes.*` published | Integration | Verdict matches actual vs predicted RPS |
| Nightly batch writes JSONL | Integration | File exists, valid JSONL, all pending outcomes present |

---

## 7. Files Changed / Created

| File | Action |
|---|---|
| `src/ppa/bus/prediction_event.py` | **New** — `PpaPredictionEvent` dataclass + serialization |
| `src/ppa/operator/state_machine.py` | Modify — add `NATSClient`, `_publish_prediction()`, observer mode in `act()` |
| `src/nexus/learning/ppa_outcome_tracker.py` | **New** — `PpaOutcomeTracker` for outcome tracking + batch flush |
| `src/nexus/predictive/prescaler.py` | Modify — subscribe to `ppa.predictions.*`; fallback hierarchy |
| `src/nexus/learning/feedback_loop.py` | Modify — wire PPA outcome → OutcomeStore |
| `deploy/crd-predictiveautoscaler.yaml` | Modify — add `observerMode` schema |
| `tests/unit/ppa/test_prediction_event.py` | **New** |
| `tests/unit/ppa/test_state_machine_observer.py` | **New** |
| `tests/integration/test_ppa_nexus_prediction_flow.py` | **New** |

---

## 8. Open Questions (resolved)

| Question | Resolution |
|---|---|
| Governance wrapping PPA or replacing it? | **Replace**: PPA in observer mode, NEXUS is sole decision authority |
| Conflict between PPA and NEXUS? | **Time-horizon separation**: PreScaler pre-scales 10min ahead; PPA reactively scales on shorter horizon |
| Feedback loop delivery | **Hybrid**: NATS event for live verdict (`ppa.outcomes.*`); nightly JSONL batch for retraining |
| PPA prediction transport | **Inline NATS publish** in kopf reconciliation via `asyncio.create_task()` |