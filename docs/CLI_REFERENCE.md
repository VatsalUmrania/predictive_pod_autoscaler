---
title: CLI Reference
description: Complete command-line reference for PPA and NEXUS CLI
last-updated: 2026-05-16
---

# CLI Reference {#cli-reference}

## PPA CLI {#ppa-commands}

### Installation {#ppa-install}

```bash
pip install -e ".[dev]"
```

---

### Operator Commands {#operator-commands}

#### `ppa operator build` {#operator-build}

Build operator Docker image.

```bash
ppa operator build [--tag TAG] [--registry REGISTRY]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--tag` | `latest` | Image tag |
| `--registry` | `local` | Docker registry |

**Example:**
```bash
ppa operator build --tag v1.0.0 --registry gcr.io/myproject
```

---

#### `ppa operator deploy` {#operator-deploy}

Deploy operator to Kubernetes cluster.

```bash
ppa operator deploy [--namespace NAMESPACE] [--image IMAGE]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--namespace` | `ppa-system` | Target namespace |
| `--image` | `ppa-operator:latest` | Operator image |

**Example:**
```bash
ppa operator deploy --namespace ppa --image gcr.io/myproject/ppa-operator:v1.0.0
```

---

#### `ppa operator status` {#operator-status}

Check operator status.

```bash
ppa operator status [--namespace NAMESPACE]
```

**Output:**
```
PPA Operator Status
-------------------
Namespace: ppa-system
Version: v1.0.0
Pods: 1/1 running
CRDs: predictiveautoscalers.autoscaling.ppa.io
```

---

#### `ppa operator logs` {#operator-logs}

Stream operator logs.

```bash
ppa operator logs [--namespace NAMESPACE] [-f] [--tail LINES]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--namespace` | `ppa-system` | Target namespace |
| `-f` | false | Follow logs |
| `--tail` | 100 | Lines to show |

**Example:**
```bash
ppa operator logs -f --tail 50
```

---

### Model Commands {#model-commands}

#### `ppa model train` {#model-train}

Train a new model.

```bash
ppa model train --app APP --horizon HORIZON [--epochs EPOCHS] [--lookback STEPS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--app` | required | Application name |
| `--horizon` | required | Prediction horizon (e.g., `rps_t10m`) |
| `--epochs` | 50 | Training epochs |
| `--lookback` | 60 | Lookback steps |

**Example:**
```bash
ppa model train --app my-app --horizon rps_t10m --epochs 100
```

---

#### `ppa model upload` {#model-upload}

Upload trained model to registry.

```bash
ppa model upload --app APP --horizon HORIZON --path PATH
```

| Option | Default | Description |
|--------|---------|-------------|
| `--app` | required | Application name |
| `--horizon` | required | Prediction horizon |
| `--path` | required | Model directory path |

**Example:**
```bash
ppa model upload --app my-app --horizon rps_t10m --path /models/my-app/rps_t10m/v1
```

---

#### `ppa model list` {#model-list}

List available models.

```bash
ppa model list [--app APP]
```

---

### Data Commands {#data-commands}

#### `ppa data collect` {#data-collect}

Collect training data.

```bash
ppa data collect --app APP --duration DURATION [--namespace NAMESPACE]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--app` | required | Application name |
| `--duration` | required | Duration (e.g., `24h`, `7d`) |
| `--namespace` | `default` | Kubernetes namespace |

---

### Utility Commands {#utility-commands}

#### `ppa add` {#add}

Add/annotate applications.

```bash
ppa add APP [--namespace NAMESPACE] [--min-replicas N] [--max-replicas N]
```

---

#### `ppa push` {#push}

Publish metrics.

```bash
ppa push [--app APP] [--namespace NAMESPACE]
```

---

## NEXUS CLI {#nexus-commands}

### Installation {#nexus-install}

```bash
pip install -e ".[nexus,llm]"
```

---

### Server Commands {#server-commands}

#### `nexus server start` {#server-start}

Start NEXUS API server.

```bash
nexus server start [--port PORT] [--host HOST]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 8000 | API port |
| `--host` | 0.0.0.0 | Bind address |

---

#### `nexus server stop` {#server-stop}

Stop NEXUS API server.

```bash
nexus server stop
```

---

### Operator Commands {#operator-commands-nexus}

#### `nexus operator start` {#nexus-operator-start}

Start NEXUS Kubernetes operator.

```bash
nexus operator start [--namespace NAMESPACE]
```

---

### Dashboard {#dashboard}

#### `nexus dashboard` {#nexus-dashboard}

Open developer dashboard.

```bash
nexus dashboard [--port PORT]
```

---

### Application Management {#app-management}

#### `nexus apps register` {#apps-register}

Register new application.

```bash
nexus apps register APP --namespace NAMESPACE --config CONFIG
```

---

#### `nexus apps list` {#apps-list}

List registered applications.

```bash
nexus apps list [--namespace NAMESPACE]
```

---

#### `nexus apps remove` {#apps-remove}

Remove application registration.

```bash
nexus apps remove APP [--namespace NAMESPACE]
```

---

## Configuration {#cli-configuration}

### Environment Variables {#cli-env}

| Variable | Default | Description |
|----------|---------|-------------|
| `PPA_PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus endpoint |
| `PPA_MODEL_DIR` | `/models` | Model directory |
| `PPA_NAMESPACE` | `default` | Default namespace |
| `PPA_KUBECONFIG` | `~/.kube/config` | Kubernetes config |
| `NEXUS_API_URL` | `http://localhost:8000` | NEXUS server URL |
| `NEXUS_API_KEY` | (none) | API authentication |

### Configuration Files {#config-files}

**PPA Config:** `~/.ppa/config.yaml`

```yaml
prometheus:
  url: http://prometheus:9090
  timeout: 2s

kubernetes:
  namespace: default
  kubeconfig: ~/.kube/config

model:
  dir: /models
  default_horizon: rps_t10m
```

**NEXUS Config:** `~/.nexus/config.yaml`

```yaml
server:
  url: http://localhost:8000
  api_key: ${NEXUS_API_KEY}

kubernetes:
  namespace: default
```

---

## Troubleshooting {#cli-troubleshooting}

### Common Issues {#common-cli-issues}

| Issue | Resolution |
|-------|------------|
| `command not found: ppa` | Ensure package is installed: `pip install -e .` |
| `KubeConfig not found` | Set `KUBECONFIG` env or use `--kubeconfig` flag |
| `Prometheus unreachable` | Check `PPA_PROMETHEUS_URL` and network |

### Debug Mode {#debug-mode}

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG

# Or use --verbose flag
ppa operator status --verbose
```

---

## See Also {#see-also}

- [Operations](OPERATIONS.md) - Operational procedures
- [Developer Guide](DEVELOPER.md) - Developer guide
- [Components](COMPONENTS.md) - Component reference
