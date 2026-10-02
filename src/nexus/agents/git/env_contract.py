"""
Environment Contract Validator
"""

from __future__ import annotations

import logging
from pathlib import Path

from nexus.agents.git.env_scanner import (
    _SYSTEM_ENV_VARS,
    extract_env_keys_js,
    extract_env_keys_python,
)

logger = logging.getLogger(__name__)


class EnvContractValidator:
    """
    Determines the 'env contract' of a codebase (what keys it requires)
    and checks whether the deployment target provides them all.
    """

    _SKIP_DIRS = {
        "venv",
        ".venv",
        "node_modules",
        ".git",
        "__pycache__",
        "dist",
        "build",
        ".next",
        "target",
        "vendor",
    }

    def __init__(
        self,
        repo_path: Path,
        env_file_paths: list[str] | None = None,
        k8s_secret_names: list[str] | None = None,
        k8s_namespace: str = "default",
    ):
        self.repo_path = repo_path
        self.env_file_paths = env_file_paths or []
        self.k8s_secret_names = k8s_secret_names or []
        self.k8s_namespace = k8s_namespace

    def required_keys(self, extensions: list[str] | None = None) -> set[str]:
        """Scan all source files and return the set of required env keys."""
        exts = extensions or [".py", ".js", ".ts", ".mjs", ".cjs"]
        found: set[str] = set()

        for ext in exts:
            for path in self.repo_path.rglob(f"*{ext}"):
                if any(skip in path.parts for skip in self._SKIP_DIRS):
                    continue
                try:
                    src = path.read_text(encoding="utf-8", errors="replace")
                    if ext == ".py":
                        found.update(extract_env_keys_python(src, str(path)))
                    else:
                        found.update(extract_env_keys_js(src))
                except OSError:
                    pass

        return found - _SYSTEM_ENV_VARS

    def available_keys(self) -> set[str]:
        """Return the union of keys from all .env files and K8s secrets."""
        available: set[str] = set()

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
                from kubernetes import client as k8s_client
                from kubernetes import config as k8s_config

                try:
                    k8s_config.load_incluster_config()
                except Exception:
                    k8s_config.load_kube_config()
                v1 = k8s_client.CoreV1Api()
                secret = v1.read_namespaced_secret(secret_name, self.k8s_namespace)
                if secret.data:
                    available.update(secret.data.keys())
                if secret.string_data:
                    available.update(secret.string_data.keys())
            except Exception as exc:
                logger.warning(
                    f"[GitAgent] Cannot read K8s secret '{secret_name}': {exc}"
                )

        return available

    def validate(self) -> dict:
        """Run the full contract validation."""
        required = self.required_keys()
        available = self.available_keys()
        missing = required - available

        logger.info(
            f"[GitAgent] Contract: {len(required)} required, "
            f"{len(available)} available, {len(missing)} missing"
        )

        return {
            "required_keys": sorted(required),
            "available_keys": sorted(available),
            "missing_keys": sorted(missing),
            "passed": len(missing) == 0,
        }
