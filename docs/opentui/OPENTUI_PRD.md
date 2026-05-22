# PRD: OpenTUI Terminal Operations Console

**Document ID:** PRD-OPENTUI-001  
**Status:** Draft  
**Created:** 2026-05-19  
**Last Updated:** 2026-05-19  
**Owner:** Engineering  
**Triage Label:** `needs-triage`

---

## Problem Statement

Operators and engineers lack real-time visibility into **what NEXUS agents are thinking and doing** during incident response. Current observability is fragmented:

- **Grafana** shows metrics but not agent reasoning
- **Logs** show events but not LLM prompts/decisions
- **Audit trail** is queryable but not live
- **No unified view** of AWS/GCP cloud events alongside agent cognition

When an incident occurs (e.g., pod OOMKill, traffic spike, bad deploy), operators must:
1. Open Grafana to see metrics
2. Check NATS logs for events
3. Query audit DB for RCA decisions
4. Manually correlate with cloud provider logs

This fragmentation increases **MTTD** (Mean Time to Detect) and **MTTR** (Mean Time to Recover), especially during multi-cloud outages.

---

## Solution

Build an **OpenTUI-based terminal UI** that provides:

1. **Live Agent Cognition Panel** — Real-time display of what each NEXUS agent is seeing, prompting, and deciding
2. **Query Interface** — Natural language queries ("Show OOMKill events last 30m") with structured results
3. **Multi-Cloud Event Stream** — Unified view of AWS, GCP, and third-party events via OpenTelemetry
4. **Scaling Decisions Timeline** — PPA + NEXUS scaling actions with confidence scores

The TUI acts as a **single pane of glass** for agent operations, complementing (not replacing) Grafana dashboards.

---

## User Stories

### Core Visibility (Option 1: Live Agent Prompts)

1. As an **on-call engineer**, I want to **see live LLM prompts from each NEXUS agent**, so that **I understand what failure each agent is diagnosing right now**.
2. As an **on-call engineer**, I want to **see confidence scores next to each active RCA**, so that **I can gauge whether to trust autonomous healing**.
3. As an **on-call engineer**, I want to **see which runbook each agent selected**, so that **I know what action will execute before it runs**.
4. As a **system architect**, I want to **trace agent decisions back to source events**, so that **I can debug why a false positive occurred**.
5. As a **reliability engineer**, I want to **see agent state transitions (IDLE → ANALYZING → ACTING → COOLDOWN)**, so that **I know if an agent is stuck or overloaded**.
6. As an **operator**, I want to **see confidence scores over time for each agent**, so that **I can detect degraded agents during partial failures**.
7. As an **operator**, I want to **see which cloud provider emitted each event**, so that **I can correlate outages with provider status pages**.
8. As an **operator**, I want to **see a timeline of all scaling decisions (PPA + NEXUS)**, so that **I can distinguish proactive from reactive scaling**.
9. As an **operator**, I want to **filter events by severity (critical/warning/info)**, so that **I can focus on incidents that need my attention**.
10. As an **operator**, I want to **see circuit breaker state for each agent**, so that **I know if an agent has tripped and stopped acting**.

### Query Interface (Option 2: Historical Queries)

11. As an **investigator**, I want to **query "Show me all OOMKill events in the last 30m"**, so that **I can understand recurrence patterns**.
12. As an **investigator**, I want to **query "Which agent handled deployment X?"**, so that **I can trace decision lineage**.
13. As an **auditor**, I want to **query all L2/L3 actions taken in the last 7 days**, so that **I can verify governance compliance**.
14. As an **engineer**, I want to **query false positives by failure class**, so that **I can prioritize runbook improvements**.
15. As a **capacity planner**, I want to **query peak scaling events by hour/day**, so that **I can plan infrastructure budgets**.
16. As an **operator**, I want to **query current cooldown states for each deployment**, so that **I know what actions are blocked**.
17. As an **operator**, I want to **query agents by health status**, so that **I can identify which agents are offline or misconfigured**.
18. As an **investigator**, I want to **export query results as JSON/CSV**, so that **I can include them in incident reports**.

### Multi-Cloud Integration

19. As a **multi-cloud operator**, I want to **see AWS CloudWatch events alongside GCP Operations events**, so that **I don't need separate dashboards**.
20. As an **operator**, I want to **see Prometheus alerts in the same timeline as NEXUS events**, so that **I can correlate metric thresholds with agent actions**.
21. As an **operator**, I want to **see NATS JetStream backlog size**, so that **I know if the event bus is lagging**.
22. As an **operator**, I want to **see OpenTelemetry trace latency histograms**, so that **I can detect slow LLM inference**.

### TUI UX Requirements

23. As a **user**, I want to **navigate panels with keyboard shortcuts**, so that **I can work without touching the mouse**.
24. As a **user**, I want to **search/filter events with fuzzy matching**, so that **I can find specific incidents quickly**.
25. As a **user**, I want to **copy selected event details to clipboard**, so that **I can paste them into incident tickets**.
26. As a **user**, I want to **auto-reconnect if the trace backend disconnects**, so that **I don't lose visibility during long sessions**.
27. As a **user**, I want to **toggle between compact and detailed views**, so that **I can fit more context on my terminal**.
28. As a **color-blind user**, I want **accessible color schemes**, so that **I can distinguish severity levels without relying on red/green**.

### Governance & Safety

29. As a **safety engineer**, I want to **see pending human-approval actions with countdown timers**, so that **I know what decisions are awaiting my input**.
30. As a **safety engineer**, I want to **manually approve/reject L3 actions from the TUI**, so that **I can accelerate or block risky operations**.
31. As a **compliance officer**, I want to **see immutable audit trail hashes**, so that **I can verify logs haven't been tampered with**.

---

## Implementation Decisions

### Architecture

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **TUI Runtime** | Bun + TypeScript | Faster cold start (<2s), native TS support, smaller bundle than Node.js |
| **TUI Framework** | OpenTUI | React-like components, Flexbox layout, modern terminal aesthetics |
| **Trace Backend** | Tempo (Grafana) | Free tier, Grafana integration, simpler ops than Jaeger |
| **Telemetry** | OpenTelemetry (OTLP) | Cloud-agnostic: AWS, GCP, Prometheus all normalize to OTLP |
| **Cloud Ingestion** | OTel Collector + Lambda (AWS) / HTTP (GCP) | Single pipeline for all providers |
| **Package Mgmt** | Bun workspaces + pip | Best-of-breed per language, no Nx/Turborepo overhead |

### Monorepo Structure

```
predictive_pod_autoscaler/
├── src/ppa/               # Python: PPA operator, ML, CLI
├── src/nexus/             # Python: NEXUS agents, RCA, governance
├── src/ui/                # TypeScript: OpenTUI TUI (NEW)
│   ├── package.json
│   ├── src/
│   │   ├── app.ts         # TUI entry point
│   │   ├── components/    # Reusable UI components
│   │   ├── panels/        # Dashboard panels
│   │   ├── queries/       # Trace backend query clients
│   │   └── utils/
│   └── README.md
├── ops/                   # Infrastructure (NEW)
│   ├── otel-collector-config.yaml
│   ├── tempo-config.yaml
│   └── docker-compose.ui.yaml
├── scripts/
│   ├── setup-monorepo.sh  # Install deps (Python + Node)
│   └── dev-ui.sh          # Start TUI dev environment
└── docs/
    ├── OPENTUI_PRD.md     # This document
    └── OPENTUI.md         # User guide
```

### Module Boundaries

The implementation will extract the following **deep modules**:

| Module | Responsibility | Why Deep? |
|--------|----------------|-----------|
| `TraceClient` | Query Tempo/Jaeger, handle retries, pagination | Encapsulates all trace backend complexity |
| `EventCorrelator` (TUI-side) | Group events by time/incident, render clusters | Local correlation for fast UI rendering |
| `AgentState` | Track live agent states (IDLE/ANALYZING/ACTING) | Derived state from trace spans |
| `QueryParser` | Parse natural language queries → TraceQL | Abstraction over query translation |
| `CloudEventNormalizer` | Map AWS/GCP schemas to common format | Vendor-specific normalization |

### Contracts

| Contract | Direction | Description |
|----------|-----------|-------------|
| `IncidentSpan` | Python → TypeScript | OpenTelemetry span format with `prompt`, `response`, `confidence` attributes |
| `QueryRequest` | TypeScript → HTTP | `{ query: string, timeRange: string, filters: {} }` |
| `QueryResponse` | HTTP → TypeScript | `{ traces: Trace[], aggregations: {} }` |
| `AgentHeartbeat` | Python → NATS → TypeScript | `{ agentId, state, lastEventTs, confidence }` |

### Risk: Critical Failure Points

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Trace backend overload** | High cardinality spans flood Tempo | Limit span attributes to 10 per incident; use `detail_level` config |
| **OpenTUI bundle too large** | Slow cold start (>3s perceived as laggy) | Code-split by panel; lazy-load query UI; preload critical CSS |
| **AWS/GCP ingestion latency** | Events delayed >10s | Buffer in OTel Collector; use OTLP batch exporter (5s interval) |
| **Python ↔ TypeScript contract drift** | Breaking changes in one break the other | Document `IncidentSpan` schema in `.proto` file; add TUI unit tests that mock Python output |
| **Bun runtime unavailable on corporate envs** | Cannot deploy TUI | Provide fallback: `docker run nexus-tui` image with Bun pre-installed |
| **TUI learns Python internals** | Tight coupling makes refactoring hard | TUI queries only trace backend API; Python internals opaque beyond `IncidentSpan` |

### Performance Budgets

| Metric | Target | Measurement |
|--------|--------|-------------|
| TUI cold start | < 2 seconds | `performance.now()` from launch to first render |
| Live update latency | < 500ms | Time from span emitted to TUI refresh |
| Query response (30m range) | < 3 seconds | P95 latency over 100 queries |
| Python agent overhead | < 5% CPU | Compare CPU with/without tracing |
| Trace backend memory | < 200MB (local dev) | Docker stats for Tempo container |

---

## Testing Decisions

### What Makes a Good Test

**Good tests verify external behavior only:**
- Trace backend returns correct traces for known queries
- TUI renders correct panels given mocked API responses
- Query parser translates natural language to valid TraceQL
- Reconnection logic restores state after disconnect

**Avoid testing implementation details:**
- Internal OpenTUI component state
- Specific CSS class names
- Exact timing of reconnection attempts (use mocks)

### Modules to Test

| Module | Test Type | Notes |
|--------|-----------|-------|
| `TraceClient` | Unit tests (Vitest) | Mock Tempo HTTP API responses |
| `QueryParser` | Unit tests (Vitest) | Test natural language → TraceQL translation |
| `CloudEventNormalizer` | Unit tests | Verify AWS/GCP schema mapping |
| `EventCorrelator` | Unit + Integration | Test clustering logic on synthetic traces |
| Full TUI | E2E (Playwright) | Simulate user navigating, querying, copying |

### Prior Art in Codebase

- Python tests: `tests/unit/nexus/test_rca_engine.py` — similar patterns for mocking LLM responses
- Docker-based tests: `tests/integration/docker-compose.test.yaml` — extends to include Tempo + TUI
- GitHub Actions workflow: `.github/workflows/docker-publish.yml` — add TypeScript build step

### Test Data

- Synthetic traces: Generate 1000 spans with known patterns (spike, OOM, config error)
- Real traces: Record production trace exports (sanitized) for regression tests

---

## Out of Scope

The following are **explicitly out of scope** for this PRD:

### Not Building (Yet)

- **Web UI alternative** — This PRD is terminal-only; web dashboard is a separate initiative
- **Mobile TUI** — iOS/Android apps not considered
- **Multi-tenant TUI** — No per-user auth/authorization; single-instance ops console
- **Alerting/Notifications** — TUI displays events but does not send emails/Slack/PagerDuty (existing Grafana rules handle this)
- **Automated remediation beyond NEXUS runbooks** — TUI is for visibility; healing remains NEXUS's responsibility
- **Historical analytics beyond 30-day retention** — Long-term analytics via separate Grafana/Tempo queries

### Not Modifying

- **NEXUS agent internals** — Agents already emit traces via OpenTelemetry (Phase 2 adds span attributes for prompts)
- **PPA reconciliation loop** — Scaling logic unchanged; TUI only displays decisions
- **Tempo backend schema** — Uses default OTLP schema; no custom Tempo plugins

---

## Further Notes

### Deployment Scenarios

| Scenario | Configuration |
|----------|---------------|
| **Local dev** | Docker Compose: TUI + Tempo + OTel Collector + mock Python agents |
| **Staging** | Kubernetes Deployment: TUI sidecar alongside Tempo; ingress for HTTP API |
| **Production** | Separate namespace; RBAC for TUI access; read-only Tempo permissions |

### Security Considerations

- **Read-only by default** — TUI queries Tempo; no write access to K8s or NEXUS
- **No secrets in TUI process** — API credentials stored in environment, not in TUI code
- **Audit log for TUI queries** — Log all queries (who queried what+when) for compliance
- **TLS for trace backend** -- Tempo HTTP API over HTTPS in production

### Accessibility

- **Color contrast** — OpenTUI theming to meet WCAG AA (4.5:1 for text)
- **Keyboard navigation** - All panels navigable without mouse
- **Screen reader compatibility** — ARIA labels on panels (future: when OpenTUI supports AT APIs)

### Future Enhancements (Deferred)

- **Collaborative cursors** — Multiple operators viewing same TUI session
- **Voice commands** — "Show me OOMKill events" via speech-to-text
- **AR/VR terminal** — Immersive incident response (experimental)

---

## Appendix: Success Criteria

| Criterion | Metric | Baseline | Target |
|-----------|--------|----------|--------|
| MTTD reduction | Mean time to detect incidents | 3 min (manual correlation) | < 30s (TUI correlation) |
| Query success rate | % queries returning valid results | N/A | > 95% |
| User satisfaction | SUS score post-deployment | N/A | > 75 |
| Adoption rate | % operators using TUI daily | 0% | > 80% |
| False positive detection | # of false positives caught via TUI | N/A | > 50% of all caught FPs |

---

## Document History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-05-19 | Engineering | Initial PRD draft |
| | | | |

---

*This PRD is linked to:*
- _Implementation Plan: [[OPENTUI_IMPLEMENTATION.md]]*
- _Architecture Decision Record: [To be created]*
- _User Guide: [docs/OPENTUI.md] (TBD)*
