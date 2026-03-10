# Operator Commands Reference

**Useful kubectl and diagnostic commands for managing and monitoring the operator**

---

## Quick Start — Deploy to Minikube

\`\`\`bash
# Full deploy: retrain, build, push artifacts, roll out
./scripts/ppa_redeploy.sh --retrain --delete-hpa

# Or deploy existing champion (no retrain):
./scripts/ppa_redeploy.sh

# Watch operator come up
kubectl logs -f deployment/ppa-operator

# Check operator health
kubectl get ppa
kubectl describe ppa test-app-ppa
\`\`\`

See **[Deployment Guide](../operator/deployment.md)** for the full flag reference.

---

## See Also

- **[Operator Architecture](../operator/architecture.md)** — How the reconciliation loop works
- **[Deployment Guide](../operator/deployment.md)** — Full deployment reference with \`ppa_redeploy.sh\`
- **[Configuration Guide](../operator/configuration.md)** — Environment variables and CR tuning
- **[Troubleshooting Guide](../operator/troubleshooting.md)** — Common issues and root causes
