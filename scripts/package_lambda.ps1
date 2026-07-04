# =============================================================================
# Package the AI Agent Lambda for deployment (Windows / PowerShell)
# =============================================================================
# Usage: pwsh scripts/package_lambda.ps1
# Run from project root: predictive_pod_autoscaler/
#
# Key difference from the bash script: pip is told to download
# manylinux x86_64 wheels so that compiled extensions (pydantic_core,
# grpcio, etc.) match Lambda's Amazon Linux 2023 runtime instead of
# picking up Windows .pyd files.

$ErrorActionPreference = "Stop"

$ROOT        = (Resolve-Path "$PSScriptRoot/..").Path
$PACKAGE_DIR = "$ROOT/lambda_package"
$ZIP_FILE    = "$ROOT/agent.zip"

Write-Host "=== NEXUS AI Agent — Lambda Packaging (Windows → Linux) ===" -ForegroundColor Cyan
Write-Host "Root: $ROOT"

# 1. Clean previous package
Write-Host "`n[1/4] Cleaning previous package..."
if (Test-Path $PACKAGE_DIR) { Remove-Item -Recurse -Force $PACKAGE_DIR }
if (Test-Path $ZIP_FILE)    { Remove-Item -Force $ZIP_FILE }
New-Item -ItemType Directory -Path $PACKAGE_DIR | Out-Null

# 2. Install Python dependencies targeting Amazon Linux 2023 (x86_64, cp312)
Write-Host "[2/4] Installing Python dependencies for linux/x86_64..."
pip install `
    --requirement "$ROOT/requirements-aws-agent.txt" `
    --target $PACKAGE_DIR `
    --platform manylinux2014_x86_64 `
    --python-version 3.12 `
    --implementation cp `
    --abi cp312 `
    --only-binary=:all: `
    --no-cache-dir `
    --quiet

if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed (exit $LASTEXITCODE)"
    exit 1
}

# 3. Copy agent source code
Write-Host "[3/4] Copying agent source..."
$awsDest = "$PACKAGE_DIR/aws"
New-Item -ItemType Directory -Path $awsDest -Force | Out-Null
Copy-Item -Recurse -Force "$ROOT/src/aws/*" $awsDest
# Ensure package marker exists
if (-not (Test-Path "$awsDest/__init__.py")) {
    New-Item -ItemType File -Path "$awsDest/__init__.py" | Out-Null
}

# 4. Trim unnecessary files
Write-Host "[3/4] Trimming package..."
Get-ChildItem -Path $PACKAGE_DIR -Recurse -Directory |
    Where-Object { $_.Name -in @("__pycache__", "tests", "test") } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Get-ChildItem -Path $PACKAGE_DIR -Recurse -Directory |
    Where-Object { $_.Name -match "\.dist-info$" } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Get-ChildItem -Path $PACKAGE_DIR -Recurse -File |
    Where-Object { $_.Extension -in @(".pyc", ".pyo") } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 5. Create zip (requires PowerShell 5+ / .NET)
Write-Host "[4/4] Creating agent.zip..."
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($PACKAGE_DIR, $ZIP_FILE)

$size = (Get-Item $ZIP_FILE).Length / 1MB
Write-Host "`n✅ Done!  agent.zip = $([math]::Round($size, 1)) MB" -ForegroundColor Green
Write-Host "   Location: $ZIP_FILE"
Write-Host "`nNext: cd infra/terraform && terraform apply"
