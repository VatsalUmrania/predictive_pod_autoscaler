# Nexus Predictive Loop — NEXUS Server + Predictive Plane Integration

**Date:** 2026-06-12
**Status:** Approved Design
**Options:** A (NexusServer class, internally supervised)

---

## Context

PPA and NEXUS both run inside Kubernetes, connected via NATS JetStream (see recent commits: `8e1eca0`). PPA's `ScalerStateMachine` publishes `PpaPredictionEvent` to `ppa.predictions.{deployment}` on every reconciliation cycle. NEXUS has all components written but no entry point — `nexus` CLI is a 10-line stub. Nothing runs.

**Goal:** Wire the predictive loop so that a PPA prediction flows through NEXUS and the full decision path is visible in shadow mode — without mutating infrastructure.

---

## 1. NATS JetStream Topology

Two isolated streams, one shared connection (via `NATSClient`):

```
PPA Operator (Kopf)          → PPA_PREDICTIONS stream (ppa.predictions.*, ppa.outcomes.*)
  state_machine.py:_schedule_nats_publish()

7 Domain Agents (sense loop) → NEXUS_INCIDENTS stream (nexus.incidents.*)
  BaseAgent.run() → nats_client.publish()

RunbookExecutor (shadow)     → nexus.actions.*
  _shadow_execute() logs only (advisory_execute() publishes via NATS)
  Notifier.start_background(nats_client) → Slack/PagerDuty

Actual scaling fires         → ppa.outcomes.{deployment}
  → PpaOutcomeTracker._check_and_emit_outcome()
    → PpaOutcomeTracker._compute_verdict() — verdict: spike_hit | spike_missed | correct_no_spike
      → PrecisionTracker.record_actual()
```

Verified methods and classes:

| Component | Method/Class | Verified at |
|---|---|---|
| PPA publishes | `ScalerStateMachine._schedule_nats_publish()` | `state_machine.py:606` |
| PPA NATS client | `PpaNATSClient` | `ppa/bus/nats_client.py` |
| Prescaler consumer | `subscribe_to_ppa_predictions()` handler closure | `prescaler.py:316-323` |
| Prescaler handler | `handle_spike_prediction()` | `prescaler.py:364` |
| Shadow execute | `_shadow_execute()` — logs only, NO NATS publish | `prescaler.py:461` |
| Advisory publish | `_advisory_execute()` — calls `self._nats.publish()` | `prescaler.py:485,512` |
| Orchestrator sub | `_on_event(event: IncidentEvent)` | `orchestrator.py:135` |
| Outcome check | `_check_and_emit_outcome()` | `ppa_outcome_tracker.py:159` |
| Verdict | `_compute_verdict()` — spike_hit / spike_missed / correct_no_spike | `ppa_outcome_tracker.py:150` |
| Notifier | `start_background(nats_client)` — sync, returns None | `notifier.py:235` |

---

## 2. Shadow Mode — Data Flow (Corrected)

### Path A: PPA Predictive Path

```
1  ScalerStateMachine._schedule_nats_publish()
     → PpaPredictionEvent published to ppa.predictions.{deployment}
2  NATS JetStream delivers event
3  subscribe_to_ppa_predictions() internal handler (prescaler.py:323) receives event
4  Prescaler.handle_spike_prediction() — records decision, checks mode
     [SHADOW] _shadow_execute() logs intent; does NOT publish to NATS
     [ADVISORY] _advisory_execute() calls self._nats.publish() → nexus.incidents.*
     [AUTONOMOUS] _autonomous_execute() calls K8s API (not in scope for this spec)
5  (shadow mode) — Orchestrator receives NOTHING from Prescaler directly
     (advisory) — Orchestrator receives via NATS nexus.incidents.*
6  NexusOrchestrator._on_event() receives IncidentEvent
7  RCA (Gemini + fallback)
8  ConfidenceScorer calibrates confidence
9  RunbookExecutor.handle_event() — logs: "Would dispatch L2: scale {dep} N→M replicas"
   (shadow) — no K8s API call, no infrastructure mutated
```

**Correction:** In shadow mode, `Prescaler._shadow_execute()` does NOT publish to `nexus.incidents.*`. The Orchestrator only receives shadow-mode decisions if a separate incident event fires from a domain agent (see Path B). Shadow-mode prescaler decisions are local to the Prescaler — visible in `/prescaler` endpoint stats but not routed to the orchestrator.

### Feedback Arc (async, separate loop)

```
A  Actual scaling fires → ppa.outcomes.{deployment}
B  PpaOutcomeTracker._check_and_emit_outcome() — polls pending predictions on interval
C  PpaOutcomeTracker._compute_verdict() — spike_hit | spike_missed | correct_no_spike
D  PrecisionTracker.record_actual() — verdict stored, PrecisionTracker.stats() updated
   → /prescaler endpoint reflects current stats
```

### Path B: Agent Detection (parallel, always running)

```
A1 Agent.sense() fires on poll_interval (default 30s)
A2 IncidentEvent returned (or [])
A3 nats_client.publish() → nexus.incidents.*
A4 NexusOrchestrator._on_event() receives — steps 6-9 same as Path A
```

---

## 3. NexusServer Architecture

### Structure

NexusServer owns all NEXUS components and manages their asyncio task lifecycle.

```python
class NexusServer:
    nats_client:      NATSClient       # shared single connection
    orchestrator:     NexusOrchestrator # RCA + governance
    prescaler:        Prescaler         # predictive scaling decisions
    outcome_tracker:  PpaOutcomeTracker # prediction outcome tracking
    notifier:         Notifier          # Slack/PagerDuty notifications
    agent_manager:    AgentManager      # manages 7 domain agents
    status_api:       FastAPI  # from nexus.observability.status_api:app
    _tasks:           list[asyncio.Task]
```

### AgentManager

```python
class AgentManager:
    """Manages the lifecycle of all 7 domain agents."""

    agents: list[BaseAgent]
    _tasks: list[asyncio.Task]

    def __init__(self, nats_client: NATSClient) -> None:
        self.agents = [
            MetricsAgent(nats_client),
            K8sAgent(nats_client),
            DBAgent(nats_client),
            NginxAgent(nats_client),
            NetworkAgent(nats_client),
            ConfigAgent(nats_client),
            GitAgent(nats_client),
        ]
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(agent.run()) for agent in self.agents]
        # start() returns immediately; _tasks holds the agent run loop Task handles.
        # On shutdown, stop() cancels these and waits for them to finish.

    async def stop(self) -> None:
        for agent in self.agents:
            agent.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
```

All agents share the same `NATSClient` instance (same JetStream connection).

### Lifecycle

```python
@classmethod
async def create(cls, nats_url: str = "nats://localhost:4222", **kwargs) -> NexusServer:
    """Only async seam. All init is sync. No background tasks in __init__."""
    nats = NATSClient(nats_url=nats_url)
    await nats.connect()

    outcome_tracker = PpaOutcomeTracker(nats_client=nats)
    # NOTE: do NOT call outcome_tracker.start() here — it is started in start() only,
    # to avoid a double polling-loop data race.

    prescaler = Prescaler(nats_client=nats)
    await prescaler.subscribe_to_ppa_predictions()

    # NexusOrchestrator requires all reasoning components — no .create() classmethod.
    from nexus.reasoning.event_correlator    import EventCorrelator
    from nexus.reasoning.rca_engine          import RCAEngine
    from nexus.reasoning.confidence_scorer   import ConfidenceScorer
    from nexus.governance.runbook_executor   import RunbookExecutor

    rca_engine        = RCAEngine()
    confidence_scorer = ConfidenceScorer()
    executor          = RunbookExecutor(nats_client=nats)
    correlator        = EventCorrelator()

    orchestrator = NexusOrchestrator(
        nats_client       = nats,
        correlator        = correlator,
        rca_engine        = rca_engine,
        confidence_scorer = confidence_scorer,
        executor          = executor,
    )

    notifier = Notifier()
    notifier.start_background(nats)  # sync (returns None); manages its own _nats_task

    agent_manager = AgentManager(nats_client=nats)

    # Import the FastAPI app; populate NexusContext so the lifespan skips self-init.
    from nexus.observability.status_api import app as status_api, context
    context.orchestrator        = orchestrator
    context.prescaler          = prescaler
    context.ppa_outcome_tracker = outcome_tracker
    # status_api lifespan will detect these are set and skip its own NATS/PPA init (see §7).

    return cls(
        nats_client    = nats,
        orchestrator   = orchestrator,
        prescaler      = prescaler,
        outcome_tracker= outcome_tracker,
        notifier       = notifier,
        agent_manager  = agent_manager,
        status_api     = status_api,
    )


async def start(self) -> None:
    # agent_manager.start() spawns agent tasks and returns immediately;
    # capture their Task handles so they can be cancelled on shutdown.
    await self.agent_manager.start()
    # outcome_tracker.start() creates the polling Task; returns without blocking.
    await self.outcome_tracker.start()

    self._tasks = [
        asyncio.create_task(self.orchestrator.start()),
        asyncio.create_task(
            uvicorn.Server(
                uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
            ).serve()
        ),
        *self.agent_manager._tasks,                       # agent run-loop Task handles
        asyncio.create_task(self.outcome_tracker._poll_task),  # proxy the tracker's poll Task
    ]
    # notifier.start_background() was called in create() — not in _tasks (sync, returns None)


async def stop(self) -> None:
    for task in self._tasks:
        task.cancel()
    await asyncio.gather(*self._tasks, return_exceptions=True)
    self.agent_manager.stop()
    self.outcome_tracker.stop()
    self.notifier.stop()          # cancels Notifier._nats_task — never skip this
    await self.nats_client.close()


async def run_forever(self) -> NoReturn:
    await asyncio.gather(*self._tasks)
```

**Critical correction:** `uvicorn.run()` is synchronous — using `asyncio.create_task(uvicorn.run(...))` raises `TypeError`. Use `uvicorn.Server(...).serve()` which IS a coroutine.

**Critical correction:** `notifier.start_background(nats_client)` is synchronous — it returns `None` and internally calls `asyncio.create_task(self._listen())`. Appending the `None` return to `self._tasks` causes `asyncio.gather(*self._tasks)` to fail. Call it before building `_tasks`, not inside it. Also: `notifier.stop()` must be called in `NexusServer.stop()` to cancel the internally-created `_nats_task`; omitting it leaks that task.

---

## 4. Files to Create

| File | Description |
|---|---|
| `src/nexus/server.py` | `NexusServer` class |
| `src/nexus/agents/manager.py` | `AgentManager` class |
| `src/nexus/cli/main.py` | Populate stub — wires `NexusServer.create()` to `nexus server start`
| `deploy/nexus/nexus-server-deployment.yaml` | K8s Deployment + Service for `NexusServer` |

### Existing files to wire in

`NexusServer.create()` instantiates and wires:
- `NATSClient` (already exists: `nexus/bus/nats_client.py`)
- `NexusOrchestrator` with `EventCorrelator`, `RCAEngine`, `ConfidenceScorer`, `RunbookExecutor` (already exist)
- `Prescaler` (already exists: `nexus/predictive/prescaler.py`)
- `PpaOutcomeTracker` (already exists: `nexus/learning/ppa_outcome_tracker.py`)
- `Notifier` (already exists: `nexus/integration/notifier.py`)
- `status_api` FastAPI app (already exists: `nexus/observability/status_api.py`)
- 7 agents (already exist in `nexus/agents/`)

---

## 5. Shadow Mode Behaviour by Prescale Mode

| Mode | Prescaler action | NATS publish | Orchestrator receives | K8s API |
|---|---|---|---|---|
| `shadow` | `_shadow_execute()` logs intent | No | Only from agents (Path B) | No |
| `advisory` | `_advisory_execute()` publishes advisory | Yes — `nexus.incidents.ORCHESTRATOR.ANOMALY_PREDICTED` | Yes | No |
| `autonomous` | `_autonomous_execute()` calls K8s API | No | Only from agents | **Yes** |

Default on first deploy: `shadow`.

---

## 6. Deployment

`NexusServer` runs as a standalone Deployment (separate from PPA operator and `nexus-api`).

Env vars:
```yaml
NATS_URL: nats://nats.default.svc.cluster.local:4222
NEXUS_MODE: shadow       # default
LOG_LEVEL: INFO
PROMETHEUS_URL: http://prometheus.default.svc.cluster.local:9090
```

Readiness: uvicorn `/health` endpoint on port 8080.

---

## 7. StatusAPI Lifespan — NexusContext Injection

`status_api.py` has its own `_lifespan` that creates a **separate** `NATSClient`, `PpaOutcomeTracker`, and `Prescaler`. If `NexusServer` mounts the same app while also creating its own instances, there will be duplicate NATS connections and two independent consumers on every subject.

**Resolution:** use the `NexusContext` injection pattern already documented in `status_api.py:41–45`.

```python
from nexus.observability.status_api import app, context

context.orchestrator        = orchestrator    # ← non-None → orchestrator endpoints work
context.prescaler          = prescaler        # ← non-None → /prescaler  endpoints work
context.ppa_outcome_tracker = outcome_tracker  # ← non-None → /learning   endpoints work
```

Before uvicorn starts, populate `context` with the instances already created by `NexusServer`. Then modify `status_api._lifespan()` to skip its own init when these fields are already set:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Skip self-init if NexusServer already populated context.
    if context.orchestrator is not None:
        # Still run integration routers and yield; no NATS/Tracker init.
        _include_integration_routers(app)
        yield
        return

    # Original self-init path (for standalone status_api runs) …
```

This gives a single NATS connection, a single `PpaOutcomeTracker`, and a single `Prescaler` consumed by both the orchestrator reasoning loop and the FastAPI status endpoints.

**Also note:** `NexusOrchestrator._on_event()` drops any event where `event.agent == AgentType.ORCHESTRATOR` (anti-loop guard at `orchestrator.py:138`). Advisory events from `_advisory_execute()` carry `agent=AgentType.ORCHESTRATOR`, so they are received by the orchestrator subscription but immediately discarded. This means the orchestrator cannot process its own pre-scale advisories — only domain agent Path B events activate the reasoning loop. This is likely intentional but should be verified.

---

## Verified Method Reference Table

All method names, signatures, and class attributions in this doc have been verified in codebase.

| What | File | Method | Note |
|---|---|---|---|
| PPA publishes | `ppa/operator/state_machine.py` | `_schedule_nats_publish()` | Daemon thread loop |
| PPA stream | `ppa/bus/nats_client.py` | `PpaNATSClient` | Stream: PPA_PREDICTIONS |
| Prescaler subscribe | `nexus/predictive/prescaler.py:316` | `subscribe_to_ppa_predictions()` | Returns immediately; sets up internal handler closure |
| Prescaler handler | `nexus/predictive/prescaler.py:364` | `handle_spike_prediction()` | CORRECTED: NOT `on_prediction()` |
| Outcome verdict | `nexus/learning/ppa_outcome_tracker.py:150` | `_compute_verdict()` | On `PpaOutcomeTracker`, not `PrecisionTracker` |
| Outcome check | `nexus/learning/ppa_outcome_tracker.py:159` | `_check_and_emit_outcome()` | Fires on interval |
| Notifier start | `nexus/integration/notifier.py:235` | `start_background(nats_client)` | Sync; returns None |
| Shutdown | `nexus/bus/nats_client.py` | `close()` | Drain + close |
| Orchestrator event | `nexus/reasoning/orchestrator.py:135` | `_on_event(event: IncidentEvent)` | Only receives `IncidentEvent`, not raw dict |
| Status API | `nexus/observability/status_api.py:38` | `uvicorn nexus.observability.status_api:app` | FastAPI app |

---

## Spec Self-Review Checklist

- [x] No invented class/method names (removed `FeedbackLoop`, fixed `on_prediction()` → `handle_spike_prediction()`)
- [x] Verdict strings match code (`spike_hit` | `spike_missed` | `correct_no_spike`)
- [x] Shadow mode does NOT publish to NATS (confirmed: `_shadow_execute()` logs only)
- [x] `uvicorn.Server(...).serve()` not `uvicorn.run()` in async context
- [x] `notifier.start_background()` called outside `_tasks` list; `notifier.stop()` called in `stop()`
- [x] `_compute_verdict()` attributed to `PpaOutcomeTracker`, not `PrecisionTracker`
- [x] Orchestrator consumes `nexus.incidents.*`, not `ppa.predictions.*` — Prescaler is correct consumer
- [x] Advisory publish goes to `nexus.incidents.ORCHESTRATOR.ANOMALY_PREDICTED` (dynamic via `event.nats_subject()`)
- [x] `NexusOrchestrator` has no `.create()` classmethod — use direct `NexusOrchestrator(...)` instantiation with all 4 required deps
- [x] `outcome_tracker.start()` called only in `start()`, not in `create()` (no double-loop)
- [x] `AgentManager` stores `_tasks` and `stop()` cancels + gathers them — no orphan agent tasks
- [x] `status_api.py` lifespan conflict resolved via `NexusContext` injection (§7)
- [x] Orchestrator anti-loop guard drops `AgentType.ORCHESTRATOR` events — advisory events won't be re-processed by orchestrator (by design, noted in §7)
- [x] CLI stub file already exists (populate, not create)

---

*Ready for writing-plans.*
