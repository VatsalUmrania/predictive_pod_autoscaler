"""
NEXUS Git Agent
================
Pre-deploy guardian and continuous deployment watcher.

Core novel capability — .env contract validation (Research gap #9):
    1. AST-scans source files for os.getenv(), process.env.X, config.get() calls
    2. Builds a complete "required env contract" for the codebase
    3. Compares against actual Kubernetes Secrets + .env files
    4. Emits ENV_CONTRACT_VIOLATION if any required key is absent

Also detects:
    - Secrets accidentally committed (regex heuristics on diff)
    - New deploys (tracks HEAD SHA changes)

Two operation modes:
    POLLING  — poll git log every interval (Phase 2, works on local repo)
    WEBHOOK  — HTTP endpoint for GitHub/GitLab push events (Phase 7)

Published IncidentEvents:
    DEPLOY_EVENT           — SHA changed (new commit / deploy triggered)
    ENV_CONTRACT_VIOLATION — required env keys missing from deployment target
    DEPLOY_BLOCKED         — accompanies ENV_CONTRACT_VIOLATION
    SECRET_COMMITTED       — potential credential detected in diff
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set

from nexus.agents.base_agent import BaseAgent
from nexus.bus.incident_event import (
    AgentType,
    DeployEventContext,
    EnvContractViolationContext,
    IncidentEvent,
    Severity,
    SignalType,
)
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Secret detection heuristics
# ──────────────────────────────────────────────────────────────────────────────

_SECRET_PATTERNS: List[tuple] = [
    (r'(?i)(api_key|api-key|apikey)\s*[=:]\s*["\'][a-zA-Z0-9_\-]{20,}["\']', "api_key"),
    (r'(?i)(secret|password|passwd|pwd)\s*[=:]\s*["\'][^"\']{8,}["\']',      "password"),
    (r'(?i)(token)\s*[=:]\s*["\'][a-zA-Z0-9_\-\.]{20,}["\']',               "token"),
    (r'AKIA[0-9A-Z]{16}',                                                     "aws_access_key"),
    (r'-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----',                     "private_key_pem"),
    (r'ghp_[a-zA-Z0-9]{36}',                                                  "github_token"),
    (r'sk-[a-zA-Z0-9]{48}',                                                   "openai_key"),
    (r'(?i)postgres://[^:]+:[^@]{8,}@',                                       "db_connection_string"),
]

_SECRET_RES = [(re.compile(p), label) for p, label in _SECRET_PATTERNS]

# System/CI environment variables to exclude from the required contract
_SYSTEM_ENV_VARS: Set[str] = {
    "PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "TERM", "TMPDIR",
    "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "CONDA_PREFIX",
    "NODE_ENV", "NODE_PATH", "DEBUG", "LOG_LEVEL", "PORT", "HOST",
    "CI", "CI_COMMIT_SHA", "GITHUB_ACTIONS", "RUNNER_OS",
}


# ──────────────────────────────────────────────────────────────────────────────
# AST-based env key extraction (Python)
# ──────────────────────────────────────────────────────────────────────────────

def extract_env_keys_python(source: str, filepath: str = "<unknown>") -> Set[str]:
    """
    Extract all env var keys referenced in Python source code via AST walking.

    Handles:
        os.getenv("KEY")              →  "KEY"
        os.getenv("KEY", default)     →  "KEY"
        os.environ.get("KEY")         →  "KEY"
        os.environ["KEY"]             →  "KEY"
        os.environ['KEY']             →  "KEY"
        getenv("KEY")                 →  "KEY"  (from os import getenv)
        settings.get("KEY")           →  skipped (heuristic: only os.*)
    """
    keys: Set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        logger.debug(f"[GitAgent] Syntax error in {filepath}, skipping AST scan")
        return keys

    for node in ast.walk(tree):
        # Function call forms
        if isinstance(node, ast.Call):
            func = node.func
            arg0 = node.args[0] if node.args else None

            # os.getenv("KEY")
            if (isinstance(func, ast.Attribute)
                    and func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"):
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

            # os.environ.get("KEY")
            elif (isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"):
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

            # getenv("KEY")  — bare call from `from os import getenv`
            elif isinstance(func, ast.Name) and func.id == "getenv":
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    keys.add(arg0.value)

        # Subscript form: os.environ["KEY"]
        elif isinstance(node, ast.Subscript):
            if (isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"):
                slice_node = node.slice
                if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                    keys.add(slice_node.value)

    return keys


# ──────────────────────────────────────────────────────────────────────────────
# Regex-based env key extraction (JavaScript / TypeScript)
# ──────────────────────────────────────────────────────────────────────────────

_ENV_JS_DOT     = re.compile(r'process\.env\.([A-Z_][A-Z0-9_]*)')
_ENV_JS_BRACKET = re.compile(r'process\.env\[[\'"]([\w]+)[\'"]\]')


def extract_env_keys_js(source: str) -> Set[str]:
    """
    Extract env var keys from JavaScript / TypeScript source.

    Handles:
        process.env.MY_KEY
        process.env['MY_KEY']
        process.env["MY_KEY"]
    """
    keys: Set[str] = set()
    keys.update(_ENV_JS_DOT.findall(source))
    keys.update(_ENV_JS_BRACKET.findall(source))
    return keys


# ──────────────────────────────────────────────────────────────────────────────
# Secret scanner
# ──────────────────────────────────────────────────────────────────────────────

def scan_diff_for_secrets(diff: str) -> List[Dict]:
    """
    Scan a git diff for accidentally committed secrets.
    Only scans added lines (prefix '+').
    Returns list of {line_number, label, snippet} for each finding.
    """
    findings = []
    for i, line in enumerate(diff.splitlines(), 1):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for regex, label in _SECRET_RES:
            if regex.search(line):
                findings.append({
                    "line_number": i,
                    "label":       label,
                    "snippet":     line[:100],
                })
    return findings


# ──────────────────────────────────────────────────────────────────────────────
# .env Contract Validator
# ──────────────────────────────────────────────────────────────────────────────

class EnvContractValidator:
    """
    Determines the 'env contract' of a codebase (what keys it requires)
    and checks whether the deployment target provides them all.

    Sources checked for available keys:
        1. .env files (local file paths)
        2. Kubernetes Secrets (by name, via K8s Python client)
    """

    _SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "__pycache__",
                  "dist", "build", ".next", "target", "vendor"}

    def __init__(
        self,
        repo_path: Path,
        env_file_paths: Optional[List[str]] = None,
        k8s_secret_names: Optional[List[str]] = None,
        k8s_namespace: str = "default",
    ):
        self.repo_path        = repo_path
        self.env_file_paths   = env_file_paths or []
        self.k8s_secret_names = k8s_secret_names or []
        self.k8s_namespace    = k8s_namespace

    # ── Required keys (code scan) ─────────────────────────────────────────────

    def required_keys(self, extensions: Optional[List[str]] = None) -> Set[str]:
        """Scan all source files and return the set of required env keys."""
        exts = extensions or [".py", ".js", ".ts", ".mjs", ".cjs"]
        found: Set[str] = set()

        for ext in exts:
            for path in self.repo_path.rglob(f"*{ext}"):
                # Skip non-source directories
                if any(skip in path.parts for skip in self._SKIP_DIRS):
                    continue
                try:
                    src = path.read_text(encoding="utf-8", errors="replace")
                    if ext == ".py":
                        found.update(extract_env_keys_python(src, str(path)))
                    else:
                        found.update(extract_env_keys_js(src))
                except (IOError, OSError):
                    pass

        return found - _SYSTEM_ENV_VARS

    # ── Available keys (env files + K8s secrets) ──────────────────────────────

    def available_keys(self) -> Set[str]:
        """Return the union of keys from all .env files and K8s secrets."""
        available: Set[str] = set()

        for env_path in self.env_file_paths:
            p = Path(env_path)
            if not p.exists():
                logger.warning(f"[GitAgent] .env not found: {p}")
                continue
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    available.add(line.split("=", 1)[0].strip())

        for secret_name in self.k8s_secret_names:
            try:
                from kubernetes import client as k8s_client, config as k8s_config
                try:
                    k8s_config.load_incluster_config()
                except Exception:
                    k8s_config.load_kube_config()
                v1     = k8s_client.CoreV1Api()
                secret = v1.read_namespaced_secret(secret_name, self.k8s_namespace)
                if secret.data:
                    available.update(secret.data.keys())
                if secret.string_data:
                    available.update(secret.string_data.keys())
            except Exception as exc:
                logger.warning(f"[GitAgent] Cannot read K8s secret '{secret_name}': {exc}")

        return available

    # ── Validate ──────────────────────────────────────────────────────────────

    def validate(self) -> Dict:
        """
        Run the full contract validation.

        Returns:
            {
                "required_keys":  sorted list of keys referenced in code
                "available_keys": sorted list of keys found in env/secrets
                "missing_keys":   sorted list of required - available
                "passed":         True if no keys are missing
            }
        """
        required  = self.required_keys()
        available = self.available_keys()
        missing   = required - available

        logger.info(
            f"[GitAgent] Contract: {len(required)} required, "
            f"{len(available)} available, {len(missing)} missing"
        )

        return {
            "required_keys":  sorted(required),
            "available_keys": sorted(available),
            "missing_keys":   sorted(missing),
            "passed":         len(missing) == 0,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Git Agent
# ──────────────────────────────────────────────────────────────────────────────

class GitAgent(BaseAgent):
    """
    Monitors a local Git repository for new commits and validates them.

    On each new commit detected:
        1. Emits DEPLOY_EVENT (always)
        2. Scans diff for accidental secrets → emits SECRET_COMMITTED
        3. Validates .env contract → emits ENV_CONTRACT_VIOLATION + DEPLOY_BLOCKED

    Args:
        repo_path:          Absolute path to the git repository root.
        env_file_paths:     .env file paths to check for available keys.
        k8s_secret_names:   K8s Secret names to check for available keys.
        k8s_namespace:      Kubernetes namespace for secret lookups.
        deployment_name:    K8s Deployment name being watched.
        poll_interval_seconds: How often to poll for new commits (default 60s).
    """

    def __init__(
        self,
        nats_client: NATSClient,
        repo_path: str,
        env_file_paths: Optional[List[str]] = None,
        k8s_secret_names: Optional[List[str]] = None,
        k8s_namespace: str = "default",
        deployment_name: str = "app",
        poll_interval_seconds: float = 60.0,
    ):
        super().__init__(
            nats_client           = nats_client,
            agent_type            = AgentType.GIT,
            poll_interval_seconds = poll_interval_seconds,
        )
        self.repo_path       = Path(repo_path)
        self.deployment_name = deployment_name
        self.k8s_namespace   = k8s_namespace
        self.validator       = EnvContractValidator(
            repo_path         = self.repo_path,
            env_file_paths    = env_file_paths or [],
            k8s_secret_names  = k8s_secret_names or [],
            k8s_namespace     = k8s_namespace,
        )
        self._last_sha: Optional[str] = None

    # ── Git helpers ───────────────────────────────────────────────────────────

    def _git(self, *args: str, timeout: int = 15) -> Optional[str]:
        """Run a git command in self.repo_path. Returns stdout or None."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.warning(f"[GitAgent] git {args[0]} failed: {exc}")
            return None

    def _head_sha(self) -> Optional[str]:
        return self._git("rev-parse", "HEAD")

    def _commit_info(self, sha: str) -> Dict:
        raw = self._git("log", "-1", "--format=%H|%an|%s|%D", sha)
        if not raw:
            return {"sha": sha, "author": "unknown", "subject": "", "refs": ""}
        parts = raw.split("|", 3)
        return {
            "sha":     parts[0] if len(parts) > 0 else sha,
            "author":  parts[1] if len(parts) > 1 else "unknown",
            "subject": parts[2] if len(parts) > 2 else "",
            "refs":    parts[3] if len(parts) > 3 else "",
        }

    def _get_diff(self, old: str, new: str) -> str:
        return self._git("diff", old, new, timeout=30) or ""

    def _changed_files(self, old: str, new: str) -> List[str]:
        raw = self._git("diff", "--name-only", old, new)
        return raw.splitlines() if raw else []

    # ── Per-commit validation ─────────────────────────────────────────────────

    async def _validate_commit(
        self, old_sha: str, new_sha: str, info: Dict
    ) -> List[IncidentEvent]:
        events: List[IncidentEvent] = []
        changed_files = self._changed_files(old_sha, new_sha)

        # 1. Always emit DEPLOY_EVENT
        events.append(IncidentEvent(
            agent=AgentType.GIT,
            signal_type=SignalType.DEPLOY_EVENT,
            severity=Severity.INFO,
            namespace=self.k8s_namespace,
            resource_name=self.deployment_name,
            resource_kind="Deployment",
            deploy_sha=new_sha,
            context=DeployEventContext(
                sha=new_sha,
                branch=info.get("refs", "unknown"),
                author=info.get("author", "unknown"),
                deployment_name=self.deployment_name,
                namespace=self.k8s_namespace,
                previous_sha=old_sha,
                changed_files=changed_files[:50],
            ).model_dump(),
        ))

        # 2. Secret scan
        diff = self._get_diff(old_sha, new_sha)
        findings = scan_diff_for_secrets(diff)
        if findings:
            logger.warning(
                f"[GitAgent] SECRET detected in {new_sha[:8]} — "
                f"labels: {[f['label'] for f in findings]}"
            )
            events.append(IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.SECRET_COMMITTED,
                severity=Severity.CRITICAL,
                namespace=self.k8s_namespace,
                resource_name=self.deployment_name,
                deploy_sha=new_sha,
                context={
                    "sha":            new_sha,
                    "author":         info.get("author"),
                    "findings":       findings[:10],
                    "total_findings": len(findings),
                },
            ))

        # 3. .env contract validation (CPU-bound — run in thread pool)
        loop       = asyncio.get_event_loop()
        validation = await loop.run_in_executor(None, self.validator.validate)

        if not validation["passed"]:
            missing = validation["missing_keys"]
            logger.warning(
                f"[GitAgent] ENV contract FAIL: {len(missing)} missing keys: "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
            events.append(IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.ENV_CONTRACT_VIOLATION,
                severity=Severity.CRITICAL,
                namespace=self.k8s_namespace,
                resource_name=self.deployment_name,
                resource_kind="Deployment",
                deploy_sha=new_sha,
                context=EnvContractViolationContext(
                    missing_keys=missing,
                    present_keys=validation["available_keys"][:50],
                    deployment_name=self.deployment_name,
                    namespace=self.k8s_namespace,
                ).model_dump(),
                suggested_runbook="runbook_missing_env_key_v1",
                suggested_healing_level=0,
                confidence=0.99,  # Deterministic check — always certain
            ))
            events.append(IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.DEPLOY_BLOCKED,
                severity=Severity.WARNING,
                namespace=self.k8s_namespace,
                resource_name=self.deployment_name,
                deploy_sha=new_sha,
                context={
                    "reason":       "env_contract_violation",
                    "missing_keys": missing,
                    "author":       info.get("author"),
                },
            ))
        else:
            logger.info(
                f"[GitAgent] .env contract OK for {new_sha[:8]} — "
                f"{len(validation['required_keys'])} keys all present"
            )

        return events

    # ── BaseAgent interface ───────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._last_sha = self._head_sha()
        logger.info(f"[GitAgent] Watching {self.repo_path} | HEAD={self._last_sha}")

    async def sense(self) -> List[IncidentEvent]:
        current = self._head_sha()
        if current is None:
            logger.warning("[GitAgent] Could not read HEAD SHA")
            return []

        if self._last_sha is None:
            self._last_sha = current
            return []

        if current == self._last_sha:
            return []   # No new commits

        logger.info(f"[GitAgent] New commit: {self._last_sha[:8]} → {current[:8]}")
        info   = self._commit_info(current)
        events = await self._validate_commit(self._last_sha, current, info)
        self._last_sha = current
        return events

    # ── Synchronous entry point for git pre-push hook ─────────────────────────

    def run_hook_validation(self) -> Dict:
        """
        Synchronous validation for use in the git pre-push hook script.
        Returns the validator result dict.
        """
        return self.validator.validate()
