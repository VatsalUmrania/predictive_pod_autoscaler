---
title: Developer Guide
description: Development workflow, testing, debugging, and contributing
last-updated: 2026-05-16
---

# Developer Guide {#developer}

## Prerequisites {#prerequisites}

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.9+ | Runtime |
| Docker | 20+ | Container builds |
| Kubernetes | 1.25+ | Local cluster (minikube/kind) |
| Prometheus | 2.35+ | Metrics |

---

## Local Development Setup {#local-setup}

### Step 1: Clone and Install {#clone-install}

```bash
git clone https://github.com/vatsalumrania/predictive-pod-autoscaler.git
cd predictive-pod-autoscaler

# Install with dev dependencies
pip install -e ".[dev]"

# Verify installation
ppa --version
pytest tests/unit -v
```

### Step 2: Set Up Local Kubernetes {#local-k8s}

```bash
# Start minikube (Linux/KVM)
minikube start --driver=kvm2 --cpus=4 --memory=8192

# Or start kind cluster
kind create cluster --config tests/kind-config.yaml

# Install Prometheus (optional)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install prometheus prometheus-community/kube-prometheus-stack
```

### Step 3: Configure Environment {#configure-env}

```bash
# .env file (for local development)
PPA_PROMETHEUS_URL=http://localhost:9090
PPA_MODEL_DIR=./model
PPA_NAMESPACE=default
LOG_LEVEL=DEBUG
```

---

## Testing {#testing}

### Test Structure {#test-structure}

```
tests/
├── unit/                  # Unit tests (fast, isolated)
│   ├── test_cli_*.py
│   ├── test_config.py
│   ├── test_features.py
│   ├── test_model_*.py
│   └── test_operator_*.py
├── integration/           # Integration tests (K8s required)
│   └── test_operator_integration.py
├── e2e/                   # End-to-end tests
│   └── test_scaling_e2e.py
└── conftest.py            # Shared fixtures
```

### Running Tests {#running-tests}

```bash
# Unit tests (fast, no K8s required)
pytest tests/unit -v

# Integration tests (K8s cluster required)
pytest tests/integration -v

# Specific test file
pytest tests/unit/test_cli.py -v

# Specific test function
pytest tests/unit/test_cli.py::test_specific_function -v

# With coverage
pytest tests/unit -v --cov=src/ppa --cov-report=html

# Type checking
mypy src/ppa --ignore-missing-imports
```

### Test Categories {#test-categories}

| Category | Command | Description |
|----------|---------|-------------|
| Unit | `pytest tests/unit` | Fast, isolated, mocks external deps |
| Integration | `pytest tests/integration` | Requires K8s, tests operator behavior |
| E2E | `make test-e2e` | Full workflow, requires demo app |
| Lint | `make lint` | Code style checks |
| Typecheck | `make typecheck` | Mypy static analysis |

---

## Code Organization {#code-organization}

### Directory Structure {#directory-structure}

```
src/
├── ppa/                   # PPA core
│   ├── cli/              # CLI implementation
│   ├── operator/         # Kubernetes operator
│   ├── model/            # ML training
│   ├── dataflow/         # Data collection
│   ├── common/           # Shared utilities
│   ├── config.py         # Configuration
│   └── domain/           # Domain models
└── nexus/                # NEXUS self-healing
    ├── agents/           # Healing agents
    ├── bus/              # Event bus
    ├── reasoning/        # RCA engine
    ├── governance/       # Policy enforcement
    ├── sdk/              # Client libraries
    └── cli/              # NEXUS CLI
```

### Adding New Components {#adding-components}

**CLI Command:**
```python
# src/ppa/cli/commands/my_command.py
import click

@click.command()
@click.option('--app', required=True, help='Application name')
def my_command(app: str):
    """My new command."""
    pass
```

**Operator Component:**
```python
# src/ppa/operator/my_component.py
class MyComponent:
    """New operator component."""
    
    def __init__(self, config):
        self.config = config
    
    def process(self):
        pass
```

---

## Debugging {#debugging}

### Debugging Operator {#debug-operator}

```bash
# View operator logs
kubectl logs -l app=ppa-operator -f

# Increase log verbosity
export LOG_LEVEL=DEBUG

# Attach debugger (development only)
kubectl debug -it pod/ppa-operator --image=python:3.9
```

### Python Debugger {#python-debugger}

```python
# Add breakpoint
import pdb; pdb.set_trace()

# Or use Python 3.7+ breakpoint
breakpoint()
```

### Remote Debugging {#remote-debugging}

```python
# VS Code remote attach configuration
# Add to launch.json:
{
    "name": "Python: Attach to Operator",
    "type": "python",
    "request": "attach",
    "connect": {
        "host": "localhost",
        "port": 5678
    },
    "pathMappings": [
        {
            "localRoot": "${workspaceFolder}",
            "remoteRoot": "/app"
        }
    ]
}
```

---

## Building and Deployment {#build-deploy}

### Build Operator Image {#build-image}

```bash
# Build Docker image
ppa operator build

# Or manually
docker build -t ppa-operator:latest -f deploy/operator/Dockerfile .
```

### Deploy to Cluster {#deploy-to-cluster}

```bash
# Deploy operator
ppa operator deploy

# Or manually
kubectl apply -f deploy/operator/

# Check status
ppa operator status

# View logs
ppa operator logs
```

---

## Contributing Guidelines {#contributing}

### Commit Message Format {#commit-format}

```
<type>: <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting
- `refactor`: Code restructuring
- `test`: Tests
- `chore`: Maintenance

### Pull Request Process {#pr-process}

1. Create feature branch from `main`
2. Make changes with tests
3. Run linting: `make lint`
4. Run tests: `pytest tests/unit -v`
5. Update documentation
6. Submit PR with description
7. Address review comments
8. Merge after approval

### Code Style {#code-style}

```bash
# Format code
black src tests

# Lint
ruff check src tests

# Type check
mypy src/ppa --ignore-missing-imports
```

---

## Architecture Decision Records (ADR) {#adr}

ADRs are stored in `docs/adr/` directory.

**Template:**
```markdown
# ADR-NNN: Title

## Status
Proposed | Accepted | Deprecated | Superseded

## Context
Why this decision was made.

## Decision
What was decided.

## Consequences
Impact of this decision.
```

---

## Common Development Tasks {#common-tasks}

### Add New Feature Flag {#feature-flag}

```python
# src/ppa/config.py
NEW_FEATURE_ENABLED = os.getenv("PPA_NEW_FEATURE", "false").lower() == "true"
```

### Add New Metric {#new-metric}

```python
# src/ppa/operator/main.py
new_metric = Gauge("ppa_new_metric", "Description", ["label"])

# In reconciliation loop
new_metric.labels(cr_name=cr_name, namespace=cr_ns).set(value)
```

### Add New CLI Command {#new-cli-command}

```python
# src/ppa/cli/commands/my_command.py
@click.command()
def my_command():
    """Command help text."""
    pass

# Register in src/ppa/cli/main.py
cli.add_command(my_command)
```

---

## Performance Guidelines {#performance}

### Inference Performance {#inference-perf}

- Target: <100ms latency
- TFLite models (not full TensorFlow)
- Batch predictions when possible

### Operator Performance {#operator-perf}

- Reconciliation interval: 30 seconds (configurable)
- CR-level error isolation (don't crash operator)
- Prometheus metrics scraping: <1s

---

## See Also {#see-also}

- [Architecture](ARCHITECTURE.md) - System overview
- [Components](COMPONENTS.md) - Component reference
- [CLI Reference](CLI_REFERENCE.md) - CLI documentation
- [Operations](OPERATIONS.md) - Operational procedures
