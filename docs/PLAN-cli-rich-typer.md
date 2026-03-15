# PPA CLI — Typer + Rich Implementation Plan

> Replace the 4 existing bash scripts with a unified, beautiful Python CLI powered by **Typer** (command framework) and **Rich** (terminal UI).

---

## Overview

The project currently relies on scattered bash scripts (`ppa_startup.sh`, `ppa_redeploy.sh`, `onboard_app.sh`, `live_comparison.sh`) that duplicate color/formatting logic, have no testability, and require users to remember multiple entry points. This plan consolidates everything into a single `ppa` CLI with rich terminal UI.

### What Already Exists

| Script | Lines | Purpose |
|--------|-------|---------|
| `ppa_startup.sh` | 412 | 11-step cluster bootstrap |
| `scripts/ppa_redeploy.sh` | 405 | Train → Convert → Deploy pipeline |
| `scripts/onboard_app.sh` | 103 | Generate PredictiveAutoscaler CRs |
| `live_comparison.sh` | 270 | Live HPA vs PPA monitoring dashboard |

### What the CLI Will Provide

- **Single entry point**: `ppa <command>` replaces all scripts
- **Rich UI**: Animated spinners, progress bars, styled tables, status panels
- **Live dashboard**: Rich `Live` display for real-time HPA vs PPA comparison
- **Pipeline integration**: Direct Python calls to `model/train.py`, `model/evaluate.py`, `model/pipeline.py` instead of subprocess shelling
- **Type-safe**: Typer handles argument parsing with type hints + autocomplete

---

## Project Type

**BACKEND / CLI** — Python CLI tool (no frontend, no mobile)

---

## Success Criteria

- [ ] `ppa --help` shows all subcommands with Rich-formatted help
- [ ] `ppa startup` replaces `ppa_startup.sh` (all 11 steps)
- [ ] `ppa deploy` replaces `scripts/ppa_redeploy.sh`
- [ ] `ppa onboard` replaces `scripts/onboard_app.sh`
- [ ] `ppa monitor` replaces `live_comparison.sh` with Rich Live dashboard
- [ ] `ppa data export` wraps `export_training_data.py`
- [ ] `ppa model train / evaluate / pipeline` wraps ML modules
- [ ] `ppa status` shows cluster health with Rich panels
- [ ] All commands show animated progress (spinners, progress bars)
- [ ] Existing shell scripts continue to work (not deleted)

---

## Tech Stack

| Technology | Purpose | Rationale |
|-----------|---------|-----------|
| **Typer** | Command framework | Type-safe, auto-complete, Click-based but modern |
| **Rich** | Terminal UI | Tables, panels, spinners, Live display, progress bars |
| **Python 3.11+** | Runtime | Already used by the project |

> **Note**: `rich` is already in `requirements.txt` (v14.3.3). Only `typer` needs to be added.

---

## File Structure

```
cli/
├── __init__.py              # Package init
├── __main__.py              # `python -m cli` entry point
├── app.py                   # Root Typer app + subcommand registration
├── config.py                # Shared CLI config (paths, defaults, colors)
├── utils.py                 # Shared Rich helpers (console, panels, run_cmd)
├── commands/
│   ├── __init__.py
│   ├── startup.py           # ppa startup  (replaces ppa_startup.sh)
│   ├── deploy.py            # ppa deploy   (replaces ppa_redeploy.sh)
│   ├── onboard.py           # ppa onboard  (replaces onboard_app.sh)
│   ├── monitor.py           # ppa monitor  (replaces live_comparison.sh)
│   ├── data.py              # ppa data export / validate
│   ├── model.py             # ppa model train / evaluate / pipeline / convert
│   └── status.py            # ppa status   (cluster health panel)
```

---

## Task Breakdown

### Phase 1: Foundation (`cli/` scaffold + shared utilities)

#### Task 1.1 — Create CLI package scaffold
- **Agent**: `devops-engineer` + `python-patterns`
- **Priority**: P0 (blocker for everything)
- **INPUT**: Project root
- **OUTPUT**: `cli/` directory with `__init__.py`, `__main__.py`, `app.py`, `config.py`, `utils.py`
- **VERIFY**: `python -m cli --help` shows the root help message
- **Details**:
  - `app.py`: Create root `typer.Typer()` with Rich help panel, register subcommand groups
  - `config.py`: Define `PROJECT_DIR`, `PROMETHEUS_URL`, port constants, color theme
  - `utils.py`: Rich `Console` singleton, helper functions:
    - `run_cmd(cmd, title)` → subprocess with Rich spinner
    - `success(msg)`, `warn(msg)`, `error(msg)` → styled console prints
    - `heading(title)` → Rich Rule/Panel
    - `kubectl(args)` → wrapper that runs kubectl with spinner
    - `wait_for_pods(label, namespace)` → spinner + kubectl wait
  - `__main__.py`: `from cli.app import app; app()`
  - Add `typer` to `requirements.txt`

#### Task 1.2 — Create a `pyproject.toml` entry point
- **Agent**: `devops-engineer`
- **Priority**: P0
- **INPUT**: `pyproject.toml` or `setup.cfg`
- **OUTPUT**: `ppa` console script entry point
- **VERIFY**: `pip install -e .` then `ppa --help` works
- **Details**:
  - Add `[project.scripts]` section: `ppa = "cli.app:app"`
  - Or keep it simple with just `python -m cli` initially

---

### Phase 2: Core Commands

#### Task 2.1 — `ppa startup` command
- **Agent**: `devops-engineer`
- **Priority**: P1
- **INPUT**: `ppa_startup.sh` logic (412 lines)
- **OUTPUT**: `cli/commands/startup.py`
- **VERIFY**: `ppa startup --list` shows all steps; `ppa startup --step 1` checks prerequisites
- **Details**:
  - Translate each of the 11 steps into Python functions
  - Use `rich.progress.Progress` for the overall pipeline (Step 1/11, Step 2/11...)
  - Each step gets a Rich `Status` spinner while running
  - `--step N` runs a single step (like the bash version)
  - `--list` shows a Rich table of all steps
  - `--dry-run` shows what would run without executing
  - Use `subprocess.run()` for kubectl/helm/docker commands, wrapped in `utils.run_cmd()`

#### Task 2.2 — `ppa deploy` command
- **Agent**: `devops-engineer`
- **Priority**: P1
- **INPUT**: `scripts/ppa_redeploy.sh` logic (405 lines)
- **OUTPUT**: `cli/commands/deploy.py`
- **VERIFY**: `ppa deploy --help` shows all options; `ppa deploy --retrain --dry-run` shows planned steps
- **Details**:
  - Translate the 9-step deploy workflow
  - `--retrain` flag triggers `model.train.train_model()` directly (Python, no subprocess)
  - `--skip-build`, `--delete-hpa`, `--keep-hpa`, `--no-watch` flags
  - Rich banner at start showing config (app, horizon, retrain status)
  - Rich step tracker (Step 1/9, Step 2/9...)
  - Final deployment summary as a Rich Panel

#### Task 2.3 — `ppa onboard` command
- **Agent**: `devops-engineer`  
- **Priority**: P1
- **INPUT**: `scripts/onboard_app.sh` logic (103 lines)
- **OUTPUT**: `cli/commands/onboard.py`
- **VERIFY**: `ppa onboard --help` shows options
- **Details**:
  - `--app-name` and `--target` as required arguments
  - Optional: `--namespace`, `--min-replicas`, `--max-replicas`, etc.
  - Generate 3 PredictiveAutoscaler CRs (3m observer, 5m observer, 10m active)
  - Apply manifests via kubectl
  - Rich table showing generated manifests and their modes

#### Task 2.4 — `ppa monitor` command (Live Dashboard)
- **Agent**: `devops-engineer`
- **Priority**: P1 (the showstopper feature)
- **INPUT**: `live_comparison.sh` logic (270 lines)
- **OUTPUT**: `cli/commands/monitor.py`
- **VERIFY**: `ppa monitor` shows a live-updating Rich dashboard
- **Details**:
  - Use `rich.live.Live` for real-time auto-refresh (every 15s)
  - Layout with Rich `Table` for each section:
    - **Current Scaling State**: HPA vs PPA desired/current replicas
    - **Real-Time Metrics**: RPS, CPU, P95 latency (from Prometheus)
    - **PPA Prediction**: t+10 prediction vs actual
    - **Validation**: Predictions from 10 minutes ago vs actual now
    - **Winner Panel**: Who's scaling better
    - **Accuracy Stats**: Overall prediction accuracy
  - Color-coded values (green for good accuracy, yellow for moderate, red for poor)
  - Keyboard interrupt (Ctrl+C) graceful exit

---

### Phase 3: ML & Data Commands

#### Task 3.1 — `ppa data` subcommand group
- **Agent**: `devops-engineer` + `python-patterns`
- **Priority**: P2
- **OUTPUT**: `cli/commands/data.py`
- **VERIFY**: `ppa data export --hours 24 --step 15s` runs export
- **Details**:
  - `ppa data export` → calls `export_training_data.build_feature_dataframe()` directly
  - `ppa data validate` → calls `validate_training_data.py` logic
  - `ppa data health` → shows dataset health as Rich table
  - Progress bar during Prometheus data collection

#### Task 3.2 — `ppa model` subcommand group
- **Agent**: `devops-engineer` + `python-patterns`
- **Priority**: P2
- **OUTPUT**: `cli/commands/model.py`
- **VERIFY**: `ppa model train --help` shows all training options
- **Details**:
  - `ppa model train` → calls `model.train.train_model()` with Rich progress for epochs
  - `ppa model evaluate` → calls `model.evaluate.evaluate_model()`, displays results as Rich table
  - `ppa model pipeline` → calls `model.pipeline.run_pipeline()` with step tracker
  - `ppa model convert` → calls `model.convert.convert_model()`
  - Each command shows a final Rich Panel with results (MAPE, MAE, model size, etc.)

---

### Phase 4: Status & Polish

#### Task 4.1 — `ppa status` command
- **Agent**: `devops-engineer`
- **Priority**: P2
- **OUTPUT**: `cli/commands/status.py`
- **VERIFY**: `ppa status` shows cluster health
- **Details**:
  - Rich Panel showing:
    - Minikube status
    - Pod status (operator, test-app, traffic-gen)
    - Port-forward health
    - Prometheus connectivity
    - PPA CR status
  - Color-coded health indicators (🟢, 🟡, 🔴)

#### Task 4.2 — Rich help & branding
- **Agent**: `devops-engineer`
- **Priority**: P3
- **OUTPUT**: Polished `app.py` with branded help
- **VERIFY**: `ppa --help` is visually stunning
- **Details**:
  - Custom Rich-themed help panel with PPA ASCII art banner
  - Version callback (`ppa --version`)
  - Typer `rich_markup_mode="rich"` for styled help text

---

## Animation & UI Specification

### Rich Components to Use

| Component | Where | Effect |
|-----------|-------|--------|
| `rich.progress.Progress` | startup, deploy, pipeline | Step-by-step progress bar with ETA |
| `rich.status.Status` | Any subprocess command | Animated spinner while running |
| `rich.live.Live` | monitor | Real-time dashboard refresh |
| `rich.table.Table` | All commands | Formatted data display |
| `rich.panel.Panel` | Results, summaries | Boxed output with titles |
| `rich.console.Console` | Everywhere | Styled text, markup |
| `rich.rule.Rule` | Section dividers | Horizontal line with title |
| `rich.tree.Tree` | File structure display | Tree view of artifacts |
| `rich.columns.Columns` | Status panels | Side-by-side layout |
| `rich.prompt.Confirm` | deploy (delete HPA?) | Interactive yes/no |

### Theme

```python
from rich.theme import Theme

PPA_THEME = Theme({
    "success": "bold green",
    "warning": "bold yellow",
    "error": "bold red",
    "info": "bold cyan",
    "step": "bold magenta",
    "metric": "bold white on dark_blue",
})
```

---

## Verification Plan

### Automated Tests

```bash
# 1. Import and help tests (no cluster required)
python -m cli --help
ppa --help
ppa startup --help
ppa deploy --help
ppa onboard --help
ppa monitor --help
ppa data --help
ppa model --help
ppa status --help

# 2. List steps (no cluster required)
ppa startup --list

# 3. Unit tests for utils
python -m pytest tests/test_cli.py -v
```

### Manual Verification

> **For the user**: Since most commands interact with kubectl/minikube, manual testing requires a running cluster.

1. Run `ppa --help` and verify the output is visually rich (panels, colors, ASCII banner)
2. Run `ppa startup --list` and verify it shows a Rich table of steps
3. Run `ppa status` and verify it shows cluster health panels
4. Run `ppa monitor` and verify the live dashboard refreshes every 15 seconds
5. Press `Ctrl+C` on `ppa monitor` and verify clean exit

---

## Phase X: Verification Checklist

- [ ] `python -m cli --help` runs without errors
- [ ] `ppa --help` shows Rich-formatted help
- [ ] All 8 subcommands show `--help` text
- [ ] `ppa startup --list` renders a Rich Table
- [ ] `ppa startup --step 1` checks prerequisites
- [ ] `ppa deploy --help` mirrors all `ppa_redeploy.sh` flags
- [ ] `ppa onboard --help` mirrors all `onboard_app.sh` flags
- [ ] `ppa monitor` shows Rich Live dashboard
- [ ] `ppa model train --help` shows training options
- [ ] `ppa data export --help` shows export options
- [ ] `ppa status` runs and displays panels
- [ ] No import errors
- [ ] `typer` added to `requirements.txt`
- [ ] Existing shell scripts NOT deleted (kept for backward compat)
