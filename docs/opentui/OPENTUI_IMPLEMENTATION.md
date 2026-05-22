# OpenTUI Integration Plan — PPA + NEXUS Monorepo

**Created:** 2026-05-19  
**Status:** Proposed  
**Owner:** Engineering Team

---

## Executive Summary

This plan integrates **OpenTUI** (TypeScript/Bun) into the PPA + NEXUS monorepo to create a cloud-agnostic agent operations console. The TUI will display:

1. **Live agent cognition state** — What each NEXUS agent is seeing, prompting, and deciding
2. **Queryable history** — Ask "What errors did Agent-2 see in last 30m?"
3. **Multi-cloud visibility** — AWS, GCP, and third-party events unified in one dashboard

**Key architecture:** Python agents emit OpenTelemetry traces → Tempo/Jaeger stores them → OpenTUI queries and displays.

---

## Current Structure

```
predictive_pod_autoscaler/
├── src/ppa/               # Python package (PPA operator, ML, CLI)
├── src/nexus/             # Python package (NEXUS agents, RCA, governance)
├── tests/                 # Python tests
├── pyproject.toml         # Python package config
└── ...
```

---

## Target Monorepo Structure

```
predictive_pod_autoscaler/
├── src/ppa/                         # Python: PPA operator, ML, CLI
├── src/nexus/                       # Python: NEXUS agents, RCA, governance
├── src/ui/                          # TypeScript: OpenTUI dashboard ★
│   ├── package.json
│   ├── bun.lock
│   ├── tsconfig.json
│   ├── src/
│   │   ├── app.ts                   # Main TUI entry
│   │   ├── components/              # Panels, widgets
│   │   ├── panels/                  # Scaling, agents, cloud events
│   │   ├── queries/                 # Trace backend queries
│   │   └── utils/
│   └── README.md
├── tests/                           # Python tests
│   └── ui/                          # TypeScript TUI tests ★
│       └── e2e/                     # Playwright or Vitest
├── ops/                             # Infrastructure ★
│   ├── otel-collector-config.yaml
│   ├── tempo-config.yaml
│   └── docker-compose.ui.yaml       # TUI + trace backend
├── scripts/
│   ├── dev-ui.sh                    # Start TUI dev server
│   └── setup-monorepo.sh            # Install Python + Node deps
├── pyproject.toml                   # Python package config
├── package.json                     # Node workspace root ★
├── bun.lock                         # TypeScript lockfile ★
└── Makefile                         # Unified build commands
```

---

## Implementation Phases

### Phase 1: Infrastructure Setup (Week 1)

| Task | Description | Effort |
|------|-------------|--------|
| 1.1 | Add `bun` runtime to repo (verify install) | 1h |
| 1.2 | Create `src/ui/` with `bun create tui` scaffold | 1h |
| 1.3 | Configure `tsconfig.json` for strict TypeScript | 30m |
| 1.4 | Add root `package.json` with workspaces | 30m |
| 1.5 | Update `.gitignore` for Node artifacts | 15m |

**Result:** Empty TypeScript TUI package in monorepo, no Python changes yet.

---

### Phase 2: OpenTelemetry in Python Agents (Week 1–2)

| Task | Description | Effort |
|------|-------------|--------|
| 2.1 | Instrument `RCAEngine` with OTel spans (prompts, decisions) | 2h |
| 2.2 | Instrument `AnalyzerAgent` with OTel spans | 2h |
| 2.3 | Instrument `HealingAgent` with OTel spans | 2h |
| 2.4 | Store LLM prompts/responses as span attributes | 3h |
| 2.5 | Export traces to local OTel Collector (Docker) | 2h |

**Key Code Change:**
```python
# src/nexus/reasoning/rca_engine.py
from opentelemetry import trace
from opentelemetry.trace import SpanKind

tracer = trace.get_tracer("nexus.rca")

async def analyze_failure(self, error_ctx):
    prompt = f"Analyzing {error_ctx.resource_type} error: {error_ctx.details}"

    with tracer.start_as_current_span("agent_analysis", kind=SpanKind.CONSUMER) as span:
        span.set_attribute("prompt", prompt)
        span.set_attribute("error_type", error_ctx.type)

        # Call LLM
        response = await self.llm_client.predict(prompt=prompt)

        span.set_attribute("response", response.decision)
        span.set_attribute("confidence", response.confidence)
        return response
```

**Result:** Python agents emit traces with full prompt/response context.

---

### Phase 3: Trace Backend + Cloud Ingestion (Week 2–3)

| Task | Description | Effort |
|------|-------------|--------|
| 3.1 | Deploy Tempo or Jaeger (Docker Compose) | 2h |
| 3.2 | Configure OTel Collector to route traces | 2h |
| 3.3 | AWS: CloudWatch Logs → Lambda → OTLP | 4h |
| 3.4 | GCP: Operations → Collector (HTTP) → OTLP | 4h |
| 3.5 | Third-party: Prometheus → OTel Collector | 2h |

**Architecture:**
```
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│ AWS         │    │ Python       │    │ GCP         │
│ CloudWatch  │───▶│ Agents (OTel)│───▶│ Operations  │
└─────────────┘    └──────────────┘    └─────────────┘
                          │
                          ▼
                   ┌──────────────────┐
                   │ OTel Collector   │
                   │ (unify + route)  │
                   └──────────────────┘
                          │
                          ▼
                   ┌──────────────────┐
                   │ Tempo / Jaeger   │
                   │ (trace backend)  │
                   └──────────────────┘
                          │
                          ▼
                   ┌──────────────────┐
                   │ OpenTUI          │
                   │ (query + display)│
                   └──────────────────┘
```

**Result:** Unified trace backend with AWS/GCP/third-party ingestion.

---

### Phase 4: OpenTUI Dashboard Build (Week 3–4)

| Task | Description | Effort |
|------|-------------|--------|
| 4.1 | Build live agent state panel (Option 1: prompts) | 4h |
| 4.2 | Build query interface panel (Option 2: queryable) | 4h |
| 4.3 | Build cloud events panel (AWS/GCP metrics) | 4h |
| 4.4 | Build scaling decisions timeline | 3h |
| 4.5 | Add keyboard navigation, focus management | 2h |
| 4.6 | Integrate LLM streaming (live prompt tokens) | 3h |

**TUI Layout Preview:**
```
┌─────────────────────────────────────────────────────────────┐
│ PPA + NEXUS Operations                               [Live] │
├─────────────────────┬──────────────────┬────────────────────┤
│ PPA Scaling         │ NEXUS Agents     │ Cloud Events       │
│ Pods: 4→7           │ 🟢 Analyzer      │ AWS: EC2 spike     │
│ Prediction: +37%    │ 🔵 RCA: L2       │ GCP: GKE p95▲      │
│ ETA: 2min           │ 🟡 Healer (idle) │ Prometheus: OK     │
├─────────────────────┴──────────────────┴────────────────────┤
│ Agent-3 Live Prompt                                         │
│ "Analyzing pod-7 OOMKill: checking memory leak pattern...   │
│  [streaming tokens in real-time]"                           │
├─────────────────────────────────────────────────────────────┤
│ Query > _                                                   │
│ > "Show OOMKill events last 30m"                            │
│ → 12:01: Agent-2 → Restart pod-7 (confidence: 87%)          │
│ → 11:47: Agent-1 → Scale HPA (confidence: 92%)              │
└─────────────────────────────────────────────────────────────┘
```

**Result:** Functional TUI showing live agent cognition + query interface.

---

### Phase 5: Monorepo Tooling + CI/CD (Week 4–5)

| Task | Description | Effort |
|------|-------------|--------|
| 5.1 | Create `scripts/setup-monorepo.sh` | 1h |
| 5.2 | Create `scripts/dev-ui.sh` (start TUI + OTel backend) | 1h |
| 5.3 | Update `Makefile` with `make ui-dev`, `make ui-build` | 30m |
| 5.4 | Add TypeScript linting (eslint + prettier) | 1h |
| 5.5 | Add Vitest for TUI unit tests | 2h |
| 5.6 | GitHub Actions: build Python + TypeScript on PR | 3h |
| 5.7 | Docker: multi-stage build for TUI + Python agents | 3h |

**New Makefile targets:**
```makefile
.PHONY: ui-install ui-dev ui-build ui-test dev-all

ui-install:
	cd src/ui && bun install

ui-dev:
	cd src/ui && bun dev

ui-build:
	cd src/ui && bun build

ui-test:
	cd src/ui && bun test

dev-all:
	docker compose -f ops/docker-compose.ui.yaml up -d
	python -m src.nexus.cli.main server start
	cd src/ui && bun dev
```

**Result:** Unified monorepo with Python + TypeScript CI/CD pipeline.

---

### Phase 6: Polish + Deploy (Week 5–6)

| Task | Description | Effort |
|------|-------------|--------|
| 6.1 | Write `src/ui/README.md` | 1h |
| 6.2 | Add TUI configuration options (theme, panels) | 2h |
| 6.3 | Error handling, reconnection logic | 2h |
| 6.4 | Performance: buffer traces, debounce queries | 2h |
| 6.5 | Documentation: `docs/OPENUI.md` | 2h |
| 6.6 | Demo scenario: multi-cloud failure + TUI visibility | 3h |

**Result:** Production-ready TUI with docs.

---

## Key Architectural Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Runtime** | Bun (not Node.js) | Faster cold start, smaller bundle, native TS |
| **TUI framework** | OpenTUI | Claude Code-like aesthetics, modern components |
| **Trace backend** | Tempo (Grafana) | Free tier, Grafana integration, simpler than Jaeger |
| **Cloud ingestion** | OTel Collector | Single pipeline for AWS/GCP/third-party |
| **Package management** | Bun + pip | Best-of-breed for each language |
| **Monorepo tooling** | Manual workspaces (no Nx/Turborepo) | Keep it simple for two languages |

---

## Build Commands

```bash
# Setup (once)
./scripts/setup-monorepo.sh

# Python dev
pip install -e ".[nexus,dev]"
ppa operator run
nexus server start

# TypeScript dev
cd src/ui
bun install
bun dev

# Start trace backend
docker compose -f ops/docker-compose.ui.yaml up

# Full dev environment
make dev-all  # Python agents + TUI + OTel + Tempo
```

---

## Git Workflow

| Branch pattern | Purpose |
|----------------|---------|
| `main` | Stable Python + TypeScript |
| `feature/ui-*` | TUI features |
| `feature/otel-*` | Telemetry instrumentation |
| **PRs** | Must pass Python tests + TypeScript lint/build |

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Two runtimes = complex CI | Use GitHub Actions matrix (Python + Node) |
| OTel trace volume high | Limit span attributes, sample low-priority traces |
| TUI learns Python internals | Contract: TUI queries only trace backend API |
| Cloud ingestion latency | Buffer in OTel Collector, batch exports |

---

## Success Criteria

| Metric | Target |
|--------|--------|
| TUI cold start | < 2 seconds |
| Live latency (prompt → TUI) | < 500ms |
| Query response (30m range) | < 3 seconds |
| Python agent overhead | < 5% CPU |
| Trace backend memory | < 200MB (local dev) |

---

## Dependencies

### Python (pyproject.toml additions)
```toml
[project.optional-dependencies]
otel = [
    "opentelemetry-api>=1.24.0",
    "opentelemetry-sdk>=1.24.0",
    "opentelemetry-exporter-otlp-proto-grpc>=1.24.0",
    "opentelemetry-exporter-otlp-proto-http>=1.24.0",
]
```

### TypeScript (src/ui/package.json)
```json
{
  "name": "@ppa-nexus/ui",
  "private": true,
  "scripts": {
    "dev": "bun run --watch src/app.ts",
    "build": "bun build src/app.ts --outdir dist",
    "test": "bun test"
  },
  "dependencies": {
    "@opentui/core": "latest"
  },
  "devDependencies": {
    "typescript": "^5.x",
    "eslint": "^8.x",
    "prettier": "^3.x"
  }
}
```

---

## Next Steps (Ordered)

1. ✅ **Approve this plan** or request adjustments
2. **Phase 1:** `bun create tui` in `src/ui/`
3. **Phase 2:** Instrument `RCAEngine` with OTel
4. **Phase 3:** Deploy Tempo + OTel Collector locally
5. **Phase 4:** Build first TUI panel (live agent state)

---

## Appendix: File Checklist

After full implementation, these files should exist:

- [ ] `src/ui/package.json`
- [ ] `src/ui/src/app.ts`
- [ ] `src/ui/tsconfig.json`
- [ ] `ops/otel-collector-config.yaml`
- [ ] `ops/tempo-config.yaml`
- [ ] `ops/docker-compose.ui.yaml`
- [ ] `scripts/setup-monorepo.sh`
- [ ] `scripts/dev-ui.sh`
- [ ] `docs/OPENUI.md`
- [ ] `.github/workflows/ci-monorepo.yml`

---

*Generated 2026-05-19*
