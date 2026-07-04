#!/bin/bash
# =============================================================================
# Package the AI Agent Lambda for deployment
# =============================================================================
# Usage: bash scripts/package_lambda.sh
# Run from project root: predictive_pod_autoscaler/

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PACKAGE_DIR="$ROOT/lambda_package"
ZIP_FILE="$ROOT/agent.zip"

echo "=== NEXUS AI Agent — Lambda Packaging ==="
echo "Root: $ROOT"

# 1. Clean previous package
rm -rf "$PACKAGE_DIR" "$ZIP_FILE"
mkdir -p "$PACKAGE_DIR"

# 2. Install Python dependencies
echo "[1/4] Installing Python dependencies..."
pip install -r "$ROOT/requirements-aws-agent.txt" \
    --target "$PACKAGE_DIR" \
    --platform manylinux2014_x86_64 \
    --only-binary=:all: \
    --implementation cp \
    --python-version 3.12 \
    --no-cache-dir \
    --quiet

# 3. Copy agent source code
echo "[2/4] Copying agent source..."
mkdir -p "$PACKAGE_DIR/aws"
cp -r "$ROOT/src/aws/"* "$PACKAGE_DIR/aws/"
touch "$PACKAGE_DIR/aws/__init__.py"

# 4. Remove unnecessary files to keep zip small
echo "[3/4] Trimming package..."
find "$PACKAGE_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$PACKAGE_DIR" -type d -name "*.dist-info" -exec rm -rf {} + 2>/dev/null || true
find "$PACKAGE_DIR" -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true

# 5. Create zip
echo "[4/4] Creating agent.zip..."
cd "$PACKAGE_DIR"
zip -r "$ZIP_FILE" . -x "*.pyc" "*.pyo" > /dev/null
cd "$ROOT"

SIZE=$(du -sh "$ZIP_FILE" | cut -f1)
echo ""
echo "✅ Done! agent.zip = $SIZE"
echo "   Location: $ZIP_FILE"
echo ""
echo "Next: cd infra/terraform && terraform apply"
