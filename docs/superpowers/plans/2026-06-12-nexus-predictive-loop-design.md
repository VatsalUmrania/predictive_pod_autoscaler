# NexusServer Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Wire NEXUS's scattered components into a runnable NexusServer with a real CLI entry point, so PPA predictions flow through
the full NEXUS reasoning loop in shadow mode from a single Kubernetes Deployment.

**Architecture:** NexusServer owns all NEXUS components and manages their asyncio task lifecycle. A single NATSClient is shared across
orchestrator, prescaler, outcome tracker, notifier, and all 7 domain agents. The FastAPI status API lifespan is short-circuited
when NexusContext is pre-populated, avoiding duplicate NATS connections. Default mode is shadow — no K8s mutations, all decisions
logged.

**Tech Stack:** Python 3.11+, asyncio, uvicorn, NATS JetStream, FastAPI, Kopf (PPA side)

---
## Bite-Sized Tasks

---
## Task 1: AgentManager — lifecycle manager for all 7 domain agents

**Files:**
- **Create:** src/nexus/agents/manager.py
- **Test:** tests/unit/nexus/test_agent_manager.py
- [ ] Step 1: Write the failing test

```python
# tests/unit/nexus/test_agent_manager.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from nexus.agents.manager import AgentManager
from nexus.agents import (
    MetricsAgent, K8sAgent, DBAgent, NginxAgent,
    NetworkAgent, ConfigAgent, GitAgent,
)


def test_agent_manager_instantiates_all_7_agents():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    assert len(manager.agents) == 7
    agent_types = {type(a).__name__ for a in manager.agents}
    expected = {
        "MetricsAgent", "K8sAgent", "DBAgent", "NginxAgent",
        "NetworkAgent", "ConfigAgent", "GitAgent",
    }
    assert agent_types == expected


@pytest.mark.asyncio
async def test_start_spawns_agent_run_tasks():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    # Patch each agent's run() to return immediately
    for agent in manager.agents:
        agent.run = AsyncMock(return_value=None)

    await manager.start()

    assert len(manager._tasks) == 7
    assert all(isinstance(t, asyncio.Task) for t in manager._tasks)


@pytest.mark.asyncio
async def test_stop_cancels_all_agent_run_tasks():
    mock_nats = MagicMock()
    manager = AgentManager(nats_client=mock_nats)

    async def slow_run():
        await asyncio.sleep(3600)

    for agent in manager.agents:
        agent.run = slow_run

    await manager.start()

    await manager.stop()

    assert manager._tasks == []
```
- [ ] Step 2: Run test to verify it fails

```text
Run: pytest tests/unit/nexus/test_agent_manager.py -v
Expected: FAIL — ModuleNotFoundError: No module named 'nexus.agents.manager'
```
- [ ] Step 3: Write the implementation

```python
# src/nexus/agents/manager.py

from __future__ import annotations

import asyncio

from nexus.agents import (
    BaseAgent,
    MetricsAgent,
    K8sAgent,
    DBAgent,
    NginxAgent,
    NetworkAgent,
    ConfigAgent,
    GitAgent,
)


class AgentManager:
    """Manages the lifecycle of all 7 domain agents.

    Each agent runs its sense→publish loop until stop() is called.
    All agents share the same NATSClient (single JetStream connection).
    """

    def __init__(self, nats_client) -> None:
        self.nats_client = nats_client
        self.agents: list[BaseAgent] = [
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
        """Spawn and store handles to all agent run-loop Tasks."""
        self._tasks = [
            asyncio.create_task(agent.run(), name=f"agent-{agent.__class__.__name__}")
            for agent in self.agents
        ]

    async def stop(self) -> None:
        """Signal all agents to stop and await their graceful shutdown."""
        for agent in self.agents:
            agent.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
```
- [ ] Step 4: Run test to verify it passes

```text
Run: pytest tests/unit/nexus/test_agent_manager.py -v
Expected: PASS
```
- [ ] Step 5: Commit

```bash
git add src/nexus/agents/manager.py tests/unit/nexus/test_agent_manager.py
git commit -m "feat(nexus): add AgentManager for 7-domain-agent lifecycle management"
```
---
## Task 2: NexusContext lifespan injection — patch status_api._lifespan

**Files:**
- **Modify:** src/nexus/observability/status_api.py:110-177
- [ ] Step 1: Write the failing test

```python
# tests/unit/nexus/test_status_api_nexus_context.py

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi.testclient import TestClient


def test_lifespan_skips_init_when_context_pre_populated():
    """When NexusContext fields are already set, lifespan must not
    create its own NATSClient / PpaOutcomeTracker / Prescaler."""
    from nexus.observability.status_api import app, context, _lifespan

    # Pre-populate context as NexusServer.create() would
    context.orchestrator = MagicMock()
    context.prescaler = MagicMock()
    context.ppa_outcome_tracker = MagicMock()
    context.audit_trail = MagicMock()
    context.feedback_loop = MagicMock()
    context.outcome_store = MagicMock()
    context.knowledge_base = MagicMock()
    context.runbook_library = MagicMock()

    started_nats: list = []
    started_tracker: list = []

    async def fake_start():
        started_nats.append(True)

    async def fake_tracker_start():
        started_tracker.append(True)

    if context.prescaler:
        context.prescaler.subscribe_to_ppa_predictions = AsyncMock()

    # Re-import to pick up patched lifespan
    import importlib
    import nexus.observability.status_api as sa_module
    importlib.reload(sa_module)

    # Manually run the lifespan
    import asyncio
    from fastapi import FastAPI
    test_app = FastAPI()
    sa_module._include_integration_routers = lambda app: None

    async def run_lifespan():
        async with sa_module._lifespan(test_app):
            # If lifespan runs its own init, started_nats/tracker would be non-empty
            pass

    asyncio.run(run_lifespan())

    # None of the self-init code should have fired
    assert len(started_nats) == 0, "lifespan created a NATSClient despite pre-populated context"
```
- [ ] Step 2: Run test to verify it fails

```text
Run: pytest tests/unit/nexus/test_status_api_nexus_context.py -v
Expected: FAIL — current lifespan always creates its own NATSClient/tracker/prescaler
```
- [ ] Step 3: Write the patch to _lifespan

In src/nexus/observability/status_api.py, replace the _lifespan function with:

```python
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Initialize async resources on startup.

    Skips self-init when NexusContext fields are already populated by NexusServer,
    to avoid duplicate NATS connections and parallel polling loops.
    """
    import logging
    import os
    _log = logging.getLogger(__name__)

    # ── Skip self-init: NexusServer already owns all components ─────────────────
    if context.orchestrator is not None:
        _log.info("[StatusAPI] NexusContext pre-populated — skipping self-init")
        _include_integration_routers(app)
        yield
        return

    # ── Self-init path (standalone status_api runs only) ──────────────────────
    try:
        from nexus.integration.token_store import get_token_store
        store = get_token_store()
        await store.init()
    except Exception as exc:
        _log.warning(f"[StatusAPI] TokenStore init skipped: {exc}")

    global _nats_client
    try:
        from nexus.bus.nats_client import NATSClient
        _nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
        _nats_client = NATSClient(nats_url=_nats_url, reconnect_attempts=10)
        await _nats_client.connect()
        _log.info(f"[StatusAPI] ✅ NATS connected: {_nats_url}")
    except Exception as exc:
        _log.warning(f"[StatusAPI] ⚠️   NATS connection failed ({exc}) — SDK events won't reach orchestrator")
        _nats_client = None

    global _ppa_outcome_tracker
    _ppa_outcome_tracker = None
    if _nats_client:
        try:
            from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
            _ppa_outcome_tracker = PpaOutcomeTracker(nats_client=_nats_client)
            await _ppa_outcome_tracker.start()
            await _nats_client.subscribe("ppa.predictions.*", cb=_on_ppa_prediction)
            _log.info("[StatusAPI] ✅ PPA OutcomeTracker started")

            try:
                from nexus.predictive.prescaler import Prescaler
                context.prescaler = Prescaler(nats_client=_nats_client)
                await context.prescaler.subscribe_to_ppa_predictions()
                _log.info("[StatusAPI] ✅ Prescaler subscribed to ppa.predictions.*")
            except Exception as exc:
                _log.warning(f"[StatusAPI] ⚠️   Prescaler init failed ({exc}) — pre-scale decisions disabled")
        except Exception as exc:
            _log.warning(f"[StatusAPI] ⚠️   PPA OutcomeTracker init failed ({exc})")

    _include_integration_routers(app)
    yield

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if _ppa_outcome_tracker:
        try:
            await _ppa_outcome_tracker.stop()
        except Exception:
            pass
    if _nats_client:
        try:
            await _nats_client.close()
        except Exception:
            pass
```
- [ ] Step 3b: Fix the import — ensure _include_integration_routers is defined before use at module level (it already is, just
verify)

```text
Run: python -c "from nexus.observability.status_api import app; print('import OK')"
```
- [ ] Step 4: Run tests to verify they pass

```text
Run: pytest tests/unit/nexus/test_status_api_nexus_context.py -v
Expected: PASS

Run: pytest tests/unit/nexus/ -v -k "status_api or prescaler" --tb=short
Expected: existing tests still pass
```
- [ ] Step 5: Commit

```bash
git add src/nexus/observability/status_api.py tests/unit/nexus/test_status_api_nexus_context.py
git commit -m "fix(nexus): skip status_api self-init when NexusContext pre-populated"
```
---
## Task 3: NexusServer.create() — async factory wiring all components

**Files:**
- **Create:** src/nexus/server.py
- **Test:** tests/unit/nexus/test_nexus_server.py
- [ ] Step 1: Write the failing test

```python
# tests/unit/nexus/test_nexus_server.py

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from nexus.server import NexusServer


def test_nexus_server_has_create_classmethod():
    assert hasattr(NexusServer, "create")
    assert callable(NexusServer.create)


@pytest.mark.asyncio
async def test_create_wires_all_components():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    mock_nats.close = AsyncMock()

    # Patch NATSClient to return mock_nats
    with patch("nexus.server.NATSClient") as MockNATSClient:
        MockNATSClient.return_value = mock_nats

        # Patch prescaler subscribe
        mock_prescaler = MagicMock()
        mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

        # Patch outcome_tracker.start() and .stop()
        mock_tracker = MagicMock()
        mock_tracker.start = AsyncMock()
        mock_tracker.stop = AsyncMock()

        # Patch agent_manager
        mock_agent_manager = MagicMock()
        mock_agent_manager.start = AsyncMock()
        mock_agent_manager.stop = MagicMock()

        # Patch notifier
        mock_notifier = MagicMock()
        mock_notifier.start_background = MagicMock()
        mock_notifier.stop = MagicMock()

        # Patch orchestrator
        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock()
        mock_orchestrator.stop = AsyncMock()

        patches = {
            "nexus.server.Prescaler": MagicMock(return_value=mock_prescaler),
            "nexus.server.PpaOutcomeTracker": MagicMock(return_value=mock_tracker),
            "nexus.server.AgentManager": MagicMock(return_value=mock_agent_manager),
            "nexus.server.Notifier": MagicMock(return_value=mock_notifier),
            "nexus.server.NexusOrchestrator": MagicMock(return_value=mock_orchestrator),
        }
        with patch.multiple("nexus.server", **patches):
            server = await NexusServer.create(nats_url="nats://mock:4222")

        assert server.nats_client is mock_nats
        assert server.prescaler is mock_prescaler
        assert server.outcome_tracker is mock_tracker
        assert server.agent_manager is mock_agent_manager
        assert server.notifier is mock_notifier
        assert server.orchestrator is mock_orchestrator

        # notifier.start_background() called with our NATS client (not in _tasks)
        mock_notifier.start_background.assert_called_once_with(mock_nats)


@pytest.mark.asyncio
async def test_create_does_not_call_outcome_tracker_start():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        mock_tracker = MagicMock()
        mock_tracker.start = MagicMock()

        with patch("nexus.server.PpaOutcomeTracker", return_value=mock_tracker):
            mock_prescaler = MagicMock()
            mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

            with patch("nexus.server.Prescaler", return_value=mock_prescaler):
                await NexusServer.create(nats_url="nats://mock:4222")

        # outcome_tracker.start() must NOT be called in create()
        # (it is called in start() only — no double-loop)
        mock_tracker.start.assert_not_called()


@pytest.mark.asyncio
async def test_start_calls_agent_manager_and_outcome_tracker():
    mock_nats = MagicMock()
    mock_nats.connect = AsyncMock()
    mock_nats.close = AsyncMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        mock_tracker = MagicMock()
        mock_tracker.start = AsyncMock()
        mock_tracker.stop = AsyncMock()
        mock_tracker._poll_task = None  # not started yet

        mock_prescaler = MagicMock()
        mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

        mock_agent_manager = MagicMock()
        mock_agent_manager.start = AsyncMock()
        mock_agent_manager._tasks = []

        mock_notifier = MagicMock()
        mock_notifier.start_background = MagicMock()
        mock_notifier.stop = MagicMock()

        mock_orchestrator = MagicMock()
        mock_orchestrator.start = AsyncMock()
        mock_orchestrator.stop = AsyncMock()

        mock_status_api = MagicMock()

        with patch.multiple(
            "nexus.server",
            PpaOutcomeTracker=MagicMock(return_value=mock_tracker),
            Prescaler=MagicMock(return_value=mock_prescaler),
            AgentManager=MagicMock(return_value=mock_agent_manager),
            Notifier=MagicMock(return_value=mock_notifier),
            NexusOrchestrator=MagicMock(return_value=mock_orchestrator),
        ):
            server = await NexusServer.create(nats_url="nats://mock:4222")

            with patch.object(server, "_tasks", []):
                mock_agent_manager._tasks = []  # pre-set to avoid attribute errors

                # Mock uvicorn.Server so start() doesn't try to bind a port
                with patch("nexus.server.uvicorn") as mock_uvicorn:
                    mock_uvicorn.Server.return_value.serve = AsyncMock()
                    await server.start()

        mock_agent_manager.start.assert_called_once()
        mock_tracker.start.assert_called_once()


@pytest.mark.asyncio
async def test_stop_cleans_up_all_subsystems():
    mock_nats = MagicMock()
    mock_nats.close = AsyncMock()
    mock_nats.connect = AsyncMock()

    mock_task = MagicMock()

    with patch("nexus.server.NATSClient", return_value=mock_nats):
        mock_tracker = MagicMock()
        mock_tracker.stop = AsyncMock()
        mock_tracker._poll_task = None

        mock_prescaler = MagicMock()
        mock_prescaler.subscribe_to_ppa_predictions = AsyncMock()

        mock_agent_manager = MagicMock()
        mock_agent_manager.stop = MagicMock()
        mock_agent_manager._tasks = []

        mock_notifier = MagicMock()
        mock_notifier.start_background = MagicMock()
        mock_notifier.stop = MagicMock()

        mock_orchestrator = MagicMock()
        mock_orchestrator.stop = AsyncMock()

        with patch.multiple(
            "nexus.server",
            PpaOutcomeTracker=MagicMock(return_value=mock_tracker),
            Prescaler=MagicMock(return_value=mock_prescaler),
            AgentManager=MagicMock(return_value=mock_agent_manager),
            Notifier=MagicMock(return_value=mock_notifier),
            NexusOrchestrator=MagicMock(return_value=mock_orchestrator),
        ):
            server = await NexusServer.create(nats_url="nats://mock:4222")
            # Simulate running tasks
            server._tasks = [MagicMock() for _ in range(3)]
            with patch("asyncio.gather", AsyncMock(return_value=[])):
                await server.stop()

        mock_notifier.stop.assert_called_once()
        mock_agent_manager.stop.assert_called()
        mock_tracker.stop.assert_called_once()
        mock_nats.close.assert_called_once()
```
- [ ] Step 2: Run test to verify it fails

```text
Run: pytest tests/unit/nexus/test_nexus_server.py -v
Expected: FAIL — ModuleNotFoundError: No module named 'nexus.server'
```
- [ ] Step 3: Write the implementation

```python
# src/nexus/server.py

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NoReturn

import uvicorn

if TYPE_CHECKING:
    import uvicorn

from nexus.agents.manager import AgentManager
from nexus.bus.nats_client import NATSClient
from nexus.governance.runbook_executor import RunbookExecutor
from nexus.integration.notifier import Notifier
from nexus.learning.ppa_outcome_tracker import PpaOutcomeTracker
from nexus.observability.status_api import app as status_api, context
from nexus.predictive.prescaler import Prescaler
from nexus.reasoning.confidence_scorer import ConfidenceScorer
from nexus.reasoning.event_correlator import EventCorrelator
from nexus.reasoning.orchestrator import NexusOrchestrator
from nexus.reasoning.rca_engine import RCAEngine

logger = logging.getLogger(__name__)


class NexusServer:
    """Owns all NEXUS components and manages their asyncio task lifecycle.

    Create via ``await NexusServer.create(...)`` — never instantiate directly.

    On startup (start()):
      - AgentManager starts 7 domain agents (sense loops, NATS publishes)
      - PpaOutcomeTracker starts its polling loop (prediction outcome resolution)
      - NexusOrchestrator starts its event-consumption loop
      - uvicorn serves the FastAPI status API on port 8080

    On shutdown (stop()):
      - All tasks are cancelled and gathered
      - AgentManager, Notifier, outcome_tracker shut down gracefully
      - NATS connection is closed
    """

    # ── Type aliases for readability ───────────────────────────────────────────

    # ── Init ───────────────────────────────────────────────────────────────────

    def __init__(
        self,
        nats_client: NATSClient,
        orchestrator: NexusOrchestrator,
        prescaler: Prescaler,
        outcome_tracker: PpaOutcomeTracker,
        notifier: Notifier,
        agent_manager: AgentManager,
        status_api,  # FastAPI app
    ) -> None:
        self.nats_client = nats_client
        self.orchestrator = orchestrator
        self.prescaler = prescaler
        self.outcome_tracker = outcome_tracker
        self.notifier = notifier
        self.agent_manager = agent_manager
        self.status_api = status_api
        self._tasks: list[asyncio.Task] = []

    # ── Factory ─────────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        nats_url: str = "nats://localhost:4222",
        runbook_dir: str | None = None,
        prometheus_url: str = "http://prometheus:9090",
        gemini_api_key: str | None = None,
        status_port: int = 8080,
    ) -> NexusServer:
        """
        Async factory: connects to NATS, creates and wires all components.

        Does NOT start any background tasks — that happens in start().

        NOTE: outcome_tracker.start() is intentionally NOT called here.
        Calling it here AND in start() would create two polling loops.
        It is started only in start().
        """
        nats = NATSClient(nats_url=nats_url)
        await nats.connect()

        # ── Prescaler ────────────────────────────────────────────────────────
        prescaler = Prescaler(nats_client=nats)
        await prescaler.subscribe_to_ppa_predictions()

        # ── Outcome tracker ───────────────────────────────────────────────────
        outcome_tracker = PpaOutcomeTracker(nats_client=nats)
        # NOTE: do NOT call outcome_tracker.start() here; see docstring above.

        # ── Reasoning components ──────────────────────────────────────────────
        from nexus.governance.audit_trail import AuditTrail
        from nexus.governance.rollback_registry import RollbackRegistry
        from nexus.governance.action_ladder import ActionLadder

        if runbook_dir is None:
            import pathlib
            runbook_dir = pathlib.Path(__file__).parent.parent.parent / "deploy" / "nexus" / "runbooks"

        audit_trail = AuditTrail()
        rollback_reg = RollbackRegistry()
        ladder = ActionLadder()
        executor = RunbookExecutor(
            nats_client=nats,
            runbook_dir=runbook_dir,
            audit_trail=audit_trail,
            action_ladder=ladder,
            rollback_registry=rollback_reg,
            prometheus_url=prometheus_url,
        )

        rca_engine = RCAEngine(api_key=gemini_api_key)
        confidence_scorer = ConfidenceScorer()
        correlator = EventCorrelator()

        orchestrator = NexusOrchestrator(
            nats_client=nats,
            correlator=correlator,
            rca_engine=rca_engine,
            confidence_scorer=confidence_scorer,
            executor=executor,
        )

        # ── Notifier (sync — manages its own background task internally) ────────
        notifier = Notifier()
        notifier.start_background(nats)  # returns None; _listen loop is internal

        # ── Agent manager ────────────────────────────────────────────────────
        agent_manager = AgentManager(nats_client=nats)

        # ── Pre-populate NexusContext so status_api lifespan skips self-init ───
        context.orchestrator = orchestrator
        context.prescaler = prescaler
        context.ppa_outcome_tracker = outcome_tracker
        context.audit_trail = audit_trail
        context.feedback_loop = None
        context.outcome_store = None
        context.knowledge_base = None
        context.runbook_library = None

        return cls(
            nats_client=nats,
            orchestrator=orchestrator,
            prescaler=prescaler,
            outcome_tracker=outcome_tracker,
            notifier=notifier,
            agent_manager=agent_manager,
            status_api=status_api,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start all background tasks.

        - agent_manager.start() spawns 7 agent run-loop Tasks and captures their handles
        - outcome_tracker.start() creates the polling Task
        - orchestrator.start() opens NATS subscriptions and begins event processing
        - uvicorn serves the FastAPI status API on port 8080
        """
        await self.agent_manager.start()

        # outcome_tracker.start() must be here, NOT in create(), to avoid a double loop.
        await self.outcome_tracker.start()

        self._tasks = [
            asyncio.create_task(self.orchestrator.start(), name="orchestrator"),
            asyncio.create_task(
                uvicorn.Server(
                    uvicorn.Config(self.status_api, host="0.0.0.0", port=8080)
                ).serve(),
                name="status-api",
            ),
            *self.agent_manager._tasks,                       # agent run-loop handles
        ]

    async def stop(self) -> None:
        """
        Graceful shutdown: cancel all tasks, stop subsystems, close NATS.
        """
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

        self.agent_manager.stop()
        await self.outcome_tracker.stop()
        self.notifier.stop()          # cancels Notifier._nats_task — never skip this
        await self.nats_client.close()

    async def run_forever(self) -> NoReturn:
        """Block until all server tasks complete (never returns normally)."""
        await asyncio.gather(*self._tasks)
```
- [ ] Step 4: Run tests to verify they pass

```text
Run: pytest tests/unit/nexus/test_nexus_server.py -v
Expected: PASS

Also verify existing tests: pytest tests/unit/nexus/ -v --tb=short
```
- [ ] Step 5: Commit

```bash
git add src/nexus/server.py tests/unit/nexus/test_nexus_server.py
git commit -m "feat(nexus): add NexusServer with full component wiring and async lifecycle"
```
---
## Task 4: CLI — wire nexus server start to NexusServer.create()

**Files:**
- **Modify:** src/nexus/cli/main.py
- **Test:** tests/unit/nexus/test_cli.py
- [ ] Step 1: Write the failing test

```python
# tests/unit/nexus/test_cli.py

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def test_cli_has_server_group():
    from nexus.cli.main import cli
    assert cli is not None
    # Should have a 'server' subcommand
    assert "server" in {c.name for c in cli.commands.values()}


def test_server_start_command_exists():
    from nexus.cli.main import cli
    server = cli.commands["server"]
    assert "start" in {c.name for c in server.commands.values()}
```
- [ ] Step 2: Run test to verify it fails

```text
Run: pytest tests/unit/nexus/test_cli.py -v
Expected: FAIL — cli doesn't exist yet
```
- [ ] Step 3: Write the CLI implementation — full main.py

Replace the contents of src/nexus/cli/main.py with:

```python
"""
NEXUS CLI

Usage:
    nexus server start [--nats-url=URL] [--port=PORT] [--prometheus-url=URL]
    nexus --version
"""

from __future__ import annotations

import os
import sys

try:
    from rich.console import Console
    from rich.table import Table
    _RICH = True
    console = Console()
except ImportError:
    _RICH = False
    console = None  # type: ignore[assignment]

import asyncio
import logging
from typing import NoReturn

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


@click.group()
@click.version_option(package_name="nexus")
def cli() -> None:
    """NEXUS self-healing infrastructure CLI."""
    pass


def _get_env_or_default(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


@cli.group()
def server() -> None:
    """NEXUS server lifecycle commands."""
    pass


@server.command()
@click.option(
    "--nats-url",
    default=lambda: _get_env_or_default("NATS_URL", "nats://localhost:4222"),
    show_default=False,
    help="NATS JetStream broker URL",
)
@click.option(
    "--port",
    default=8080,
    show_default=True,
    help="Port for the FastAPI status API",
)
@click.option(
    "--prometheus-url",
    default=lambda: _get_env_or_default("PROMETHEUS_URL", "http://prometheus:9090"),
    show_default=False,
    help="Prometheus URL for metrics scraping",
)
@click.option(
    "--runbook-dir",
    default=None,
    help="Override path to runbook YAML files",
)
@click.option(
    "--gemini-api-key",
    default=lambda: os.environ.get("GEMINI_API_KEY"),
    help="Gemini API key for LLM-powered RCA",
)
@click.option(
    "--log-level",
    default=lambda: os.environ.get("LOG_LEVEL", "INFO"),
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    help="Logging level",
)
def start(
    nats_url: str,
    port: int,
    prometheus_url: str,
    runbook_dir: str | None,
    gemini_api_key: str | None,
    log_level: str,
) -> NoReturn:
    """Start the NEXUS server (NexusServer)."""
    # Apply log level
    logging.getLogger().setLevel(getattr(logging, log_level.upper()))

    async def _run() -> NoReturn:
        from nexus.server import NexusServer

        nats_url_val = nats_url() if callable(nats_url) else nats_url
        prom_url_val = prometheus_url() if callable(prometheus_url) else prometheus_url
        gemini_key = gemini_api_key() if callable(gemini_api_key) else gemini_api_key

        if _RICH:
            console.print(f"[bold green]Starting NexusServer[/bold green]  NATS={nats_url_val}")
            console.print(f"  Prometheus={prom_url_val}  Status API={port}")

        try:
            server = await NexusServer.create(
                nats_url=nats_url_val,
                runbook_dir=runbook_dir,
                prometheus_url=prom_url_val,
                gemini_api_key=gemini_key,
                status_port=port,
            )
            await server.start()

            if _RICH:
                console.print("[bold green]✅ NexusServer running[/bold green]")
                console.print(f"  Status API: http://0.0.0.0:{port}")
                console.print("  Press Ctrl+C to stop")

            # Block until interrupted
            while True:
                await asyncio.sleep(3600)
        except KeyboardInterrupt:
            if _RICH:
                console.print("\n[yellow]Received interrupt — shutting down[/yellow]")
            sys.exit(0)
        except Exception as exc:
            log.exception("NexusServer failed to start: %s", exc)
            sys.exit(1)

    asyncio.run(_run())


def main() -> int:
    """Entry point registered as the `nexus` console script."""
    try:
        cli()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        log.exception("CLI error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```
- [ ] Step 4: Run tests to verify they pass

```text
Run: pytest tests/unit/nexus/test_cli.py -v
Expected: PASS

Verify the CLI is callable without errors:
Run: python -c "from nexus.cli.main import cli; print('CLI imports OK')"
```
- [ ] Step 5: Commit

```bash
git add src/nexus/cli/main.py tests/unit/nexus/test_cli.py
git commit -m "feat(nexus): wire nexus server start CLI to NexusServer.create()"
```
---
## Task 5: Kubernetes Deployment manifest for NexusServer

**Files:**
- **Create:** deploy/nexus/nexus-server-deployment.yaml
- **Modify:** deploy/nexus/ (optional — add note to existing README)
- [ ] Step 1: Write the manifest

```yaml
# deploy/nexus/nexus-server-deployment.yaml
---
# NexusServer — the NEXUS reasoning-plane entry point.
# Runs alongside PPA operator and the existing nexus-api deployment.
# Default mode is shadow (no K8s mutations, all decisions logged).

apiVersion: v1
kind: ConfigMap
metadata:
  name: nexus-server
  namespace: default
data:
  LOG_LEVEL: "INFO"
  NEXUS_MODE: "shadow"   # shadow | advisory | autonomous
  PROMETHEUS_URL: "http://prometheus:9090"
  NEXUS_RUNBOOK_DIR: "/runbooks"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexus-server
  namespace: default
  labels:
    app: nexus-server
    component: reasoning-plane
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nexus-server
  template:
    metadata:
      labels:
        app: nexus-server
        component: reasoning-plane
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8080"
        prometheus.io/path: "/metrics"
    spec:
      serviceAccountName: nexus-server
      containers:
        - name: nexus-server
          image: nexus:latest           # replace with real image tag in CI
          imagePullPolicy: IfNotPresent
          args:
            - server
            - start
            - --nats-url=nats://nats:4222
            - --port=8080
            - --prometheus-url=http://prometheus:9090
            - --log-level=$(LOG_LEVEL)
          env:
            - name: NATS_URL
              value: "nats://nats:4222"
            - name: LOG_LEVEL
              valueFrom:
                configMapKeyRef:
                  name: nexus-server
                  key: LOG_LEVEL
            - name: NEXUS_MODE
              valueFrom:
                configMapKeyRef:
                  name: nexus-server
                  key: NEXUS_MODE
            - name: PROMETHEUS_URL
              valueFrom:
                configMapKeyRef:
                  name: nexus-server
                  key: PROMETHEUS_URL
            - name: GEMINI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: nexus-secrets          # create via: kubectl create secret generic nexus-secrets 
--from-literal=GEMINI_API_KEY=your-key
                  key: gemini-api-key
                  optional: true
          ports:
            - name: http
              containerPort: 8080
              protocol: TCP
          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 10
            periodSeconds: 30
            timeoutSeconds: 5
          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "1Gi"
          volumeMounts:
            - name: runbooks
              mountPath: /runbooks
              readOnly: true
      volumes:
        - name: runbooks
          configMap:
            name: nexus-runbooks          # create separately; maps runbook YAML files
      terminationGracePeriodSeconds: 30

---
apiVersion: v1
kind: Service
metadata:
  name: nexus-server
  namespace: default
  labels:
    app: nexus-server
spec:
  type: ClusterIP
  ports:
    - name: http
      port: 8080
      targetPort: http
      protocol: TCP
  selector:
    app: nexus-server

---
# ServiceAccount (used for K8s API calls in autonomous mode — harmless in shadow mode)
apiVersion: v1
kind: ServiceAccount
metadata:
  name: nexus-server
  namespace: default

---
# RBAC: allow read-only access to pods/deployments in autonomous mode
# Scope to specific deployments/services as needed for your environment.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: nexus-server
  namespace: default
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods", "services"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: nexus-server
  namespace: default
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: nexus-server
subjects:
  - kind: ServiceAccount
    name: nexus-server
    namespace: default
```
- [ ] Step 2: Verify YAML is valid

```text
Run: python -c "import yaml; yaml.safe_load_all(open('deploy/nexus/nexus-server-deployment.yaml')); print('YAML valid')"
```
- [ ] Step 3: Commit

```bash
git add deploy/nexus/nexus-server-deployment.yaml
git commit -m "feat(k8s): add nexus-server Deployment, Service, and RBAC manifest"
```
---
## Task 6: Verify the full integration — smoke test

**Files:**
- **Create:** tests/integration/test_nexus_server_smoke.py
- [ ] Step 1: Write the smoke test

```python
# tests/integration/test_nexus_server_smoke.py
"""
Smoke test: NexusServer.create() + start() + stop() with a real-ish NATS mock.
Requires: NATS server running locally, or skip if NATS unavailable.
"""

import pytest

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("SKIP_NATS_TESTS") == "1",
    reason="SKIP_NATS_TESTS=1; no NATS server available",
)


@pytest.mark.asyncio
async def test_nexus_server_lifecycle_with_nats(nats_server):
    """Full create→start→stop cycle against a real NATS server."""
    from nexus.server import NexusServer

    nats_url = str(nats_server.uri)

    server = await NexusServer.create(
        nats_url=nats_url,
        runbook_dir=None,
        prometheus_url="http://prometheus:9090",
        gemini_api_key=None,
    )

    # server should have all components wired
    assert server.nats_client is not None
    assert server.orchestrator is not None
    assert server.prescaler is not None
    assert server.outcome_tracker is not None
    assert server.notifier is not None
    assert server.agent_manager is not None
    assert server.status_api is not None

    # start() should not raise
    await server.start()

    # Confirm agent tasks are running
    assert len(server._tasks) >= 1  # orchestrator + status-api + agents

    # stop() should not raise
    await server.stop()

    assert server._tasks == []
```
- [ ] Step 2: Run with a real NATS (or confirm skip message)

```text
Run: SKIP_NATS_TESTS=1 pytest tests/integration/test_nexus_server_smoke.py -v
Expected: SKIPPED
```
- [ ] Step 3: Commit

```bash
git add tests/integration/test_nexus_server_smoke.py
git commit -m "test(integration): add NexusServer lifecycle smoke test"
```
---
## Task 7: Final verification

- [ ] Step 1: Run all nexus unit tests

```bash
pytest tests/unit/nexus/ -v --tb=short
```
- [ ] Step 2: Check no regressions in full suite

```bash
pytest tests/unit/ -v --tb=short -x
```
- [ ] Step 3: Verify imports are clean

```bash
python -c "from nexus.server import NexusServer; from nexus.agents.manager import AgentManager; from nexus.cli.main import cli; 
print('All imports OK')"
```
---
## Spec Coverage Checklist

| Spec requirement | Task |
| --- | --- |
| NexusServer class with create() / start() / stop() / run_forever() | Task 3 |
| Shared NATSClient across all components | Task 3 |
| AgentManager spawns and cancels 7 agent tasks | Task 1 |
| Prescaler subscribed in create() | Task 3 |
| PpaOutcomeTracker.start() NOT called in create() (no double loop) | Task 2, Task 3 |
| notifier.start_background() called outside _tasks, notifier.stop() in stop() | Task 3 |
| uvicorn.Server(...).serve() (not uvicorn.run()) | Task 3 |
| NexusContext pre-population so status_api skips self-init | Task 2 |
| nexus server start CLI entry point wired to NexusServer.create() | Task 4 |
| K8s Deployment + Service + RBAC manifest | Task 5 |
| Integration smoke test | Task 6 |

---
## Type Consistency Check

| Item | Defined in | Used in | Consistent |
| --- | --- | --- | --- |
| PpaOutcomeTracker.start() | Task 3 | Task 3 (in start()), never in create() | ✅ |
| PpaOutcomeTracker.stop() | Task 3 | Task 3 (in stop()) | ✅ |
| Notifier.start_background(nats) | codebase | Task 3 (called outside _tasks) | ✅ |
| Notifier.stop() | codebase | Task 3 (in stop()) | ✅ |
| NexusOrchestrator constructor | codebase | Task 3 | ✅ |
| EventCorrelator, RCAEngine, ConfidenceScorer, RunbookExecutor | codebase | Task 3 | ✅ |
| uvicorn.Server(Config).serve() | Task 3 | Task 3 | ✅ |
| 7 agent types in AgentManager.__init__ | Task 1 | Task 1 | ✅ |

---
## Plan complete. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration

2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints
