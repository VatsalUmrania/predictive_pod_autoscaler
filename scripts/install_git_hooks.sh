#!/usr/bin/env bash
# =============================================================================
# NEXUS Git Pre-Push Hook Installer
# =============================================================================
# Installs a pre-push hook into the current git repository that runs the
# GitAgent's .env contract validation before every push.
#
# If any required environment variable is missing from the deployment target,
# the push is BLOCKED with a clear error message listing the missing keys.
#
# Usage:
#   chmod +x scripts/install_git_hooks.sh
#   ./scripts/install_git_hooks.sh
#
# To uninstall:
#   rm .git/hooks/pre-push
# =============================================================================

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="${REPO_ROOT}/.git/hooks"
HOOK_FILE="${HOOK_DIR}/pre-push"

# ── Detect project root for Python path ──────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

echo "🔧 NEXUS Git Hook Installer"
echo "   Repo root:    ${REPO_ROOT}"
echo "   Project root: ${PROJECT_ROOT}"

# ── Write pre-push hook ───────────────────────────────────────────────────────
cat > "${HOOK_FILE}" << 'HOOK_SCRIPT'
#!/usr/bin/env bash
# NEXUS pre-push hook — .env contract validation
# Auto-installed by scripts/install_git_hooks.sh

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
NEXUS_HOOK_PYTHON="${NEXUS_HOOK_PYTHON:-python3}"

# Find the nexus package
PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"
export PYTHONPATH

echo ""
echo "🛡️  NEXUS: Running .env contract validation before push..."
echo ""

# Run inline Python validation
"${NEXUS_HOOK_PYTHON}" - << 'PYTHON_EOF'
import sys
import os

# Ensure src/ is in path
import pathlib
repo_root = pathlib.Path(os.environ.get("REPO_ROOT", "."))
src_path  = repo_root / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

try:
    from nexus.agents.git_agent import EnvContractValidator
except ImportError as exc:
    print(f"⚠️  NEXUS not importable ({exc}) — skipping validation")
    sys.exit(0)

# Build validator from environment
env_files      = [f for f in [
    os.getenv("NEXUS_ENV_FILE", os.path.join(str(repo_root), ".env")),
    os.getenv("NEXUS_ENV_FILE_PROD"),
] if f and pathlib.Path(f).exists()]

k8s_secrets    = [s.strip() for s in os.getenv("NEXUS_K8S_SECRETS", "").split(",") if s.strip()]
k8s_namespace  = os.getenv("NEXUS_K8S_NAMESPACE", "default")

validator = EnvContractValidator(
    repo_path         = repo_root,
    env_file_paths    = env_files,
    k8s_secret_names  = k8s_secrets,
    k8s_namespace     = k8s_namespace,
)

result = validator.validate()

if result["missing_keys"]:
    print("❌  PUSH BLOCKED by NEXUS — missing required env keys:")
    print()
    for key in result["missing_keys"]:
        print(f"    • {key}")
    print()
    print("📋  Required keys found in code: " + str(len(result["required_keys"])))
    print("✅  Keys available in env/secrets: " + str(len(result["available_keys"])))
    print()
    print("Fix: Add missing keys to your .env file or Kubernetes Secret,")
    print("     then retry the push.")
    print()
    print("To bypass (not recommended): git push --no-verify")
    sys.exit(1)
else:
    print(f"✅  NEXUS: .env contract OK — {len(result['required_keys'])} required keys all present")
    print()
    sys.exit(0)
PYTHON_EOF

# Check Python exit code
if [ $? -ne 0 ]; then
    exit 1
fi
HOOK_SCRIPT

# Make executable
chmod +x "${HOOK_FILE}"

echo "✅ Pre-push hook installed at: ${HOOK_FILE}"
echo ""
echo "Configuration (optional env vars for the hook):"
echo "    NEXUS_ENV_FILE       — path to .env file (default: <repo>/.env)"
echo "    NEXUS_ENV_FILE_PROD  — path to production .env (optional)"
echo "    NEXUS_K8S_SECRETS    — comma-separated Kubernetes Secret names"
echo "    NEXUS_K8S_NAMESPACE  — Kubernetes namespace (default: default)"
echo "    NEXUS_HOOK_PYTHON    — Python executable (default: python3)"
echo ""
echo "To test the hook without pushing:"
echo "    bash .git/hooks/pre-push"
echo ""
echo "To uninstall:"
echo "    rm .git/hooks/pre-push"
