# AGENTIC DIRECTIVE

> This file is identical to AGENTS.md. Keep them in sync.

## CODING ENVIRONMENT

- This repo uses **pip + a project venv** with a `Makefile` as the canonical entrypoint. There is no `uv`, no `uv.lock`, no `[tool.uv]` table — do not introduce them.
- Python `>=3.10` per `pyproject.toml` (`[project] requires-python`). Do not bump to 3.14-syntax features.
- Install once with `make install-dev` (which runs `pip install -e ".[dev]"`). Extras (`[operator]`, `[model]`, `[nexus]`, `[gemini]`, `[openai]`, `[anthropic]`, `[llm]`, `[all]`) compose the same way: `pip install -e ".[dev,nexus]"`.
- Read `demo/backend/.env.example` for dev-time environment variables. Path/env contracts for the operator and NEXUS live in `src/ppa/config.py` — that is the **only** module allowed to read `os.environ` directly.
- All CI checks must pass; failing checks block merge. The CI gates are:
  - `ruff check src tests` (lint)
  - `mypy src/ppa --ignore-missing-imports` (type check)
  - `pytest tests/unit -v` (unit tests)
- Format: `black src tests` then `ruff check --fix src tests` — they are complementary (black formats, ruff auto-fixes the rest). See the `Makefile` `format` target.
- Add tests for new changes (including edge cases). Tests live in `tests/unit/` (mock external deps), `tests/integration/` (real K8s), `tests/e2e/` (full streaming pipeline). Pick the tier whose failure mode you'd actually want to catch.
- Before pushing, prefer the local CI sequence:
  ```bash
  make format      # black + ruff --fix
  make lint        # ruff check + mypy
  make typecheck   # mypy
  make test        # pytest tests/unit -v
  ```
  Use individual targets (`make test`, `make lint`) when iterating on one stage. There is no `scripts/ci.sh` / `.ps1` cross-platform wrapper — keep using `make`. On CI, the same gates run non-repair-mode in `.github/workflows/`.
- `pyproject.toml` already configures mypy for leniency (`ignore_missing_imports = true`, `disable_error_code = ["import-untyped"]`). **Do not add more `# type: ignore` / `# mypy: ignore` comments** to silence failures — fix the underlying type or tighten the boundary to make it narrow naturally.
- Branch protection should require all of: the `ruff`/lint job, the `mypy`/typecheck job, and the `pytest` job (use the exact labels GitHub shows in `.github/workflows/`; they may be prefixed with `CI /`).

## IDENTITY & CONTEXT

- You are an expert Software Architect and Systems Engineer.
- This repository is **PPA + NEXUS** — a Kubernetes-resident predictive autoscaler (PPA) coupled with a self-healing control plane (NEXUS) of seven monitoring agents, LLM-powered RCA, and an L0–L3 ActionLadder governing healing.
- Goal: zero-defect, root-cause-oriented engineering for bugs; test-driven engineering for new features. Think carefully; no need to rush.
- Code: write the simplest code possible. Keep the codebase minimal and modular.

## ARCHITECTURE PRINCIPLES

- **Shared utilities**: Put shared forecasting and reasoning primitives in neutral owners — `ppa/common/` for feature spec, types, PromQL helpers; `nexus/reasoning/` for LLM/RCA logic. Do not have a provider or agent import shared helpers from another agent's module.
- **Cross-plane boundary**: `src/nexus/` must not import from `src/ppa/`. PPA ↔ NEXUS communication happens exclusively via the NATS event bus in `src/nexus/bus/`. If you find a NEXUS module reaching into PPA directly, that's a layering bug.
- **DRY**: Extract shared base classes to eliminate duplication. Prefer composition over copy-paste. (Example: the seven agents all extend `src/nexus/agents/base_agent.py` — new patterns of cross-agent behavior belong there, not duplicated.)
- **Encapsulation**: Use accessor methods for internal state (e.g. storing state on a typed holder), not direct `_attribute` assignment from outside.
- **Provider-specific config**: Keep provider-specific fields (LLM provider credentials, NATS URL, NATS subject prefix) in the provider's constructor or in `src/ppa/config.py` — never as hardcoded literals in agent / operator modules.
- **Dead code**: Remove unused code, legacy systems, and hardcoded values. Use settings/config instead of literals (e.g. `get_config().llm_provider` not `"gemini"`). Same rule for action levels, agent names, and any cross-module identifier.
- **Performance**: Use list accumulation for strings (not `+=` in loops), cache env vars at init (`get_config()` is the entrypoint), prefer iterative over recursive when stack depth matters. Cooldown stores and audit trails are persistent — never put them on a hot-path map that resets on operator restart.
- **Platform-agnostic naming**: Use generic names (e.g. `EVENT_PUBLISH`) in cross-module code, not agent-specific ones (`K8S_AGENT_PUBLISH`). Subjects are declared in one place inside `src/nexus/bus/nats_client.py`; subjects invented inside an agent are a refactor target.
- **No type ignores**: Do not add `# type: ignore` or `# mypy: ignore`. Fix the underlying type. The project's mypy config already accounts for untyped third-party deps.
- **Complete migrations**: When moving modules, update imports to the new owner in the same change and remove old compatibility shims unless preserving a published interface is explicitly required. Half-migrations rot.
- **Maximum Test Coverage**: There should be maximum test coverage for everything, preferably live smoke test coverage (driven by `tests/e2e/` against a real cluster, plus `tests/integration/` for K8s-bound flows) to catch bugs early. Unit tests in `tests/unit/` mock external dependencies.

## COGNITIVE WORKFLOW

1. **ANALYZE**: Read relevant files. Then read every caller of the function you're about to touch — the bug is usually one frame away from the report. Do not guess.
2. **PLAN**: Map out the logic. Identify root cause or required changes. Order changes by dependency.
3. **EXECUTE**: Fix the cause, not the symptom. A guard in the shared function is a smaller diff than a guard per caller — and patching only the path the ticket names leaves every sibling caller still broken. Execute incrementally with clear commits.
4. **VERIFY**: Run the local sequence (`make format && make lint && make typecheck && make test`), plus relevant smoke/integration tests when needed. Confirm via logs or output.
5. **SPECIFICITY**: Do exactly as much as asked; nothing more, nothing less.
6. **PROPAGATION**: Changes impact multiple files; propagate updates correctly (rename refactor updates every import site, schema changes touch train/export/predictor in lockstep).
7. **VERSION**: If the commit touches production files on `main`, bump semver in the same commit (see [Versioning](#versioning-main)).

## VERSIONING (MAIN)

Every commit on `main` that changes a **production file** must include a semver bump in **`pyproject.toml`** (`[project].version`) in the **same commit**. Do not merge or push prod changes without updating the version.

### Production files

These paths count as production (runtime, packaging, or install surface):

- `src/ppa/` (operator, model, dataflow, runtime, infrastructure, common, domain, cli, config)
- `src/nexus/` (agents, reasoning, governance, bus, observability, telemetry, integration, predictive, learning, sdk, server, cli)
- `deploy/` (`crd.yaml`, `rbac.yaml`, `operator-deployment.yaml`, `operator-servicemonitor.yaml`, templates, `nexus/`, `opa/`, `demo/`, manifests under `generated-manifests/`)
- `pyproject.toml` (dependencies, scripts, packaging)
- `requirements.txt`, `requirements.operator.txt` (when changed)
- `Makefile`
- `scripts/install_git_hooks.sh`

These do **not** require a version bump on their own:

- `tests/` (any tier)
- Docs and assets: `README.md`, `docs/`, `AGENTS.md`, `CLAUDE.md`, `ARCHITECTURE.md`, `logo.png`
- CI and repo config: `.github/`, `.gitignore`, `.dockerignore`, `mcp.json`
- Local-only: `demo/`, `colab/`, `.vscode/`, `.claude/`, `.agents/`

If a single commit mixes production and non-production edits, still bump the version.

### Semver rules

Use `[project].version` (currently `2.0.0`) as `MAJOR.MINOR.PATCH`:

- **PATCH** (`x.y.Z+1`): bug fixes, refactors with no user-visible behavior change, dependency updates, packaging/install fixes.
- **MINOR** (`x.Y+1.0`): backward-compatible features—new agents, new action-ladder levels, new CLI commands, new config keys, or behavior additions.
- **MAJOR** (`X+1.0.0`): breaking changes—removed/renamed env vars, changed CRD fields, incompatible CLI/default changes, or migrations users must act on.

When unsure between PATCH and MINOR, prefer PATCH for fixes and MINOR for new capability.

### Required steps

1. Classify the change and choose the bump level.
2. Update `version` in `pyproject.toml`.
3. There is no separate lockfile (the project uses `setuptools` with pinned ranges in `pyproject.toml[project.dependencies]`). When bumping or adding a release-coupling dep, re-pin in `[project.dependencies]` and update `requirements.txt` / `requirements.operator.txt` to match.
4. Include the version and lockfile (or requirements) updates in the same commit as the production change.

Example commit on `main` after a packaging fix: bump `2.0.0` → `2.0.1`, update `requirements.txt` to match, commit together with the fix.

## SUMMARY STANDARDS

- Summaries must be technical and granular.
- Include: `[Files Changed]`, `[Logic Altered]`, `[Verification Method]`, `[Residual Risks]` (if no residual risks then say none).

## TOOLS

- Prefer built-in tools (`grep`, `find`, `Read`, `Edit`, `Write`, `Bash`) over manual workflows. Check tool availability before use.
- `pytest -v --tb=short` (configured in `pyproject.toml`); use `make test` as the standard entrypoint.
- `ruff check src tests`, `black src tests`, `mypy src/ppa --ignore-missing-imports` — match the `pyproject.toml` config; do not invent tighter per-call rules.
- NATS events: route through `src/nexus/bus/`; never handcraft `publish`/`subscribe` from inside an agent.
