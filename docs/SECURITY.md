---
title: Security Guide
description: Authentication, authorization, RBAC, and security best practices
last-updated: 2026-05-16
---

# Security Guide {#security}

## Authentication {#authentication}

### PPA Authentication {#ppa-auth}

PPA operator uses Kubernetes RBAC for authentication and authorization.

#### ServiceAccount {#serviceaccount}

The operator runs with a dedicated `ServiceAccount`:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ppa-operator
  namespace: ppa-system
```

#### RBAC Permissions {#rbac}

**Source:** `deploy/operator/rbac.yaml`

The operator requires these permissions:

| Resource | Verbs | Purpose |
|----------|-------|---------|
| `deployments` | `get`, `list`, `watch`, `patch` | Scale deployments |
| `predictiveautoscalers` | `get`, `list`, `watch` | Watch CRs |
| `predictiveautoscalers/status` | `update`, `patch` | Update CR status |
| `configmaps` | `get` | Load configuration |
| `secrets` | `get` | Access model credentials |

**Minimal RBAC:**
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ppa-operator
rules:
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["get", "list", "watch", "patch"]
- apiGroups: ["autoscaling.ppa.io"]
  resources: ["predictiveautoscalers"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["autoscaling.ppa.io"]
  resources: ["predictiveautoscalers/status"]
  verbs: ["update", "patch"]
```

---

### NEXUS Authentication {#nexus-auth}

#### API Authentication {#api-auth}

NEXUS server supports these authentication methods:

**1. API Key Authentication** {#api-key}

```python
# Configuration
NEXUS_API_KEY=<secret-key>

# Client request
curl -H "Authorization: Bearer $NEXUS_API_KEY" http://nexus-server/api/incidents
```

**2. Service Account Token** {#sa-token}

```python
# Kubernetes service account
from kubernetes.client import Configuration
config = Configuration()
config.api_key = {'authorization': f"Bearer {sa_token}"}
```

---

## Authorization {#authorization}

### RBAC Roles {#rbac-roles}

#### PPA Operator Roles {#ppa-rbac}

| Role | Permissions | Use Case |
|------|-------------|----------|
| `ppa-operator` | Full PPA CRD access | Operator pod |
| `ppa-viewer` | Read-only CRD access | Dashboard, monitoring |
| `ppa-admin` | Full access to PPA resources | Human operators |

**Viewer Role:**
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ppa-viewer
rules:
- apiGroups: ["autoscaling.ppa.io"]
  resources: ["predictiveautoscalers"]
  verbs: ["get", "list", "watch"]
```

#### NEXUS Roles {#nexus-rbac}

| Role | Permissions | Use Case |
|------|-------------|----------|
| `nexus-operator` | Full NEXUS access | Operator pod |
| `nexus-agent` | Event publish, read incidents | Healing agents |
| `nexus-viewer` | Read-only incidents | Dashboards |
| `nexus-admin` | Full access including approvals | Human operators |

---

## Secret Management {#secret-management}

### Sensitive Data {#sensitive-data}

| Secret | Type | Storage |
|--------|------|---------|
| `NEXUS_GEMINI_API_KEY` | LLM API key | Kubernetes Secret |
| `NEXUS_OPENAI_API_KEY` | LLM API key | Kubernetes Secret |
| `ANTHROPIC_API_KEY` | LLM API key | Kubernetes Secret |
| `NEXUS_API_KEY` | API auth | Kubernetes Secret |

### Storing Secrets {#storing-secrets}

```bash
# Create secret
kubectl create secret generic nexus-secrets \
  --from-literal=gemini-api-key="${GEMini_API_KEY}" \
  --from-literal=nexus-api-key="${NEXUS_API_KEY}" \
  -n nexus-system

# Reference in deployment
# deploy/nexus/deployment.yaml
env:
- name: NEXUS_GEMINI_API_KEY
  valueFrom:
    secretKeyRef:
      name: nexus-secrets
      key: gemini-api-key
```

### Best Practices {#secret-best-practices}

1. **Never commit secrets** to version control
2. **Use Kubernetes Secrets** or external secret management (Vault, AWS Secrets Manager)
3. **Rotate API keys** regularly
4. **Limit secret access** to necessary pods only
5. **Audit secret access** via audit logs

---

## Network Security {#network-security}

### Network Policies {#network-policies}

**Source:** `deploy/operator/network-policy.yaml`

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ppa-operator-policy
  namespace: ppa-system
spec:
  podSelector:
    matchLabels:
      app: ppa-operator
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: monitoring
    ports:
    - protocol: TCP
      port: 9100  # Prometheus metrics
  - from:
    - namespaceSelector:
        matchLabels:
          name: kube-system
    ports:
    - protocol: TCP
      port: 8080  # Health check
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: prometheus
    ports:
    - protocol: TCP
      port: 9090  # Prometheus API
  - to:
    - namespaceSelector: {}
    ports:
    - protocol: TCP
      port: 443  # Kubernetes API
```

### Allowed Connections {#allowed-connections}

| Source | Destination | Port | Purpose |
|--------|-------------|------|---------|
| Prometheus | Operator | 9100 | Metrics scraping |
| Kubelet | Operator | 8080 | Liveness probe |
| Operator | Prometheus | 9090 | Query metrics |
| Operator | K8s API | 443 | Watch/patch resources |

---

## Audit Logging {#audit-logging}

### Operator Audit Trail {#operator-audit}

The operator logs all scaling decisions:

```python
# src/ppa/operator/main.py:1412
logger.info(
    f"[{cr_name}] Scaling {target_ns}/{target}: {current} → {desired}"
)
```

**Log format:**
```
2026-05-16 12:34:56,789 [ppa.operator] INFO [my-app-ppa] Scaling default/my-app: 2 → 4
```

### NEXUS Audit Database {#nexus-audit}

**Source:** `src/nexus/learning/audit.db`

NEXUS maintains an audit trail in SQLite:

```sql
-- Incident audit trail
SELECT
    i.id,
    i.created_at,
    i.rca_root_cause,
    a.action_type,
    a.result,
    a.executed_at
FROM incidents i
LEFT JOIN actions a ON i.id = a.incident_id
ORDER BY i.created_at DESC;
```

### Log Aggregation {#log-aggregation}

For compliance, forward logs to:
- **ELK Stack** (Elasticsearch, Logstash, Kibana)
- **Splunk**
- **CloudWatch Logs**
- **Datadog**

---

## Security Best Practices {#best-practices}

### Operator Security {#operator-security}

1. **Run as non-root user**
   ```yaml
   securityContext:
     runAsNonRoot: true
     runAsUser: 1000
   ```

2. **Disable unnecessary features**
   ```yaml
   spec:
     observerMode: true  # Read-only mode
   ```

3. **Limit RBAC permissions** to minimum required

4. **Use resource quotas**
   ```yaml
   resources:
     requests:
       cpu: "100m"
       memory: "256Mi"
     limits:
       cpu: "500m"
       memory: "512Mi"
   ```

### NEXUS Security {#nexus-security}

1. **Enable authentication** for all API endpoints
2. **Use TLS** for all communications
3. **Rate limit** API requests
4. **Validate input** on all endpoints
5. **Audit all actions** in SQLite database

### Model Security {#model-security}

1. **Validate model artifacts** before loading
2. **Use signed models** (verify checksums)
3. **Isolate model directory** with proper permissions
4. **Limit model upload** to authorized users only

---

## Incident Response {#incident-response}

### Recovering from Security Incidents {#incident-recovery}

1. **Identify scope**: Check audit logs for unusual activity
2. **Rotate credentials**: All API keys and tokens
3. **Review RBAC**: Ensure no unauthorized access
4. **Check model integrity**: Verify model checksums
5. **Review scaling history**: Check for unauthorized scaling

### Emergency Procedures {#emergency}

```bash
# Disable operator (stop scaling)
kubectl scale deployment ppa-operator --replicas=0 -n ppa-system

# Switch to observer mode
kubectl patch predictiveautoscaler my-app-ppa \
  --type merge -p '{"spec":{"observerMode":true}}'

# Reset credentials
kubectl delete secret nexus-secrets -n nexus-system
kubectl create secret generic nexus-secrets \
  --from-literal=gemini-api-key="${NEW_API_KEY}" \
  -n nexus-system
kubectl rollout restart deployment/nexus-server -n nexus-system
```

---

## Compliance {#compliance}

### SOC 2 Controls {#soc2}

| Control | Implementation |
|---------|----------------|
| Access Control | RBAC + API authentication |
| Audit Trail | SQLite audit logs |
| Change Detection | Model versioning, Git history |
| Time Synchronization | Kubernetes cluster time |

### GDPR Considerations {#gdpr}

- Audit logs may contain PII if application names include user data
- Configure log retention policy appropriately
- Implement data deletion on request

---

## See Also {#see-also}

- [[OPERATIONS.md#configuration]] - Configuration reference
- [Architecture](ARCHITECTURE.md) - System architecture
- [Kubernetes RBAC](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/)
