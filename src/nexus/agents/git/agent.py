"""
NEXUS Git Agent Implementation
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from nexus.agents.base_agent import BaseAgent
from nexus.agents.git.commit_validator import validate_commit
from nexus.agents.git.env_contract import EnvContractValidator
from nexus.bus.incident_event import AgentType, IncidentEvent
from nexus.bus.nats_client import NATSClient

logger = logging.getLogger(__name__)


class GitAgent(BaseAgent):
    """
    Monitors a local Git repository for new commits and validates them.
    """

    def __init__(
        self,
        nats_client: NATSClient,
        repo_path: str,
        env_file_paths: list[str] | None = None,
        k8s_secret_names: list[str] | None = None,
        k8s_namespace: str = "default",
        deployment_name: str = "app",
        poll_interval_seconds: float = 60.0,
    ):
        super().__init__(
            nats_client=nats_client,
            agent_type=AgentType.GIT,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.repo_path = Path(repo_path)
        self.deployment_name = deployment_name
        self.k8s_namespace = k8s_namespace
        self.validator = EnvContractValidator(
            repo_path=self.repo_path,
            env_file_paths=env_file_paths or [],
            k8s_secret_names=k8s_secret_names or [],
            k8s_namespace=k8s_namespace,
        )
        self._last_sha: str | None = None

    def _git(self, *args: str, timeout: int = 15) -> str | None:
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

    def _head_sha(self) -> str | None:
        return self._git("rev-parse", "HEAD")

    def _commit_info(self, sha: str) -> dict:
        raw = self._git("log", "-1", "--format=%H|%an|%s|%D", sha)
        if not raw:
            return {"sha": sha, "author": "unknown", "subject": "", "refs": ""}
        parts = raw.split("|", 3)
        return {
            "sha": parts[0] if len(parts) > 0 else sha,
            "author": parts[1] if len(parts) > 1 else "unknown",
            "subject": parts[2] if len(parts) > 2 else "",
            "refs": parts[3] if len(parts) > 3 else "",
        }

    def _get_diff(self, old: str, new: str) -> str:
        return self._git("diff", old, new, timeout=30) or ""

    def _changed_files(self, old: str, new: str) -> list[str]:
        raw = self._git("diff", "--name-only", old, new)
        return raw.splitlines() if raw else []

    async def _validate_commit(
        self, old_sha: str, new_sha: str, info: dict
    ) -> list[IncidentEvent]:
        diff = self._get_diff(old_sha, new_sha)
        changed_files = self._changed_files(old_sha, new_sha)
        return await validate_commit(
            validator=self.validator,
            repo_path=self.repo_path,
            deployment_name=self.deployment_name,
            k8s_namespace=self.k8s_namespace,
            old_sha=old_sha,
            new_sha=new_sha,
            info=info,
            diff=diff,
            changed_files=changed_files,
        )

    async def on_start(self) -> None:
        self._last_sha = self._head_sha()
        logger.info(f"[GitAgent] Watching {self.repo_path} | HEAD={self._last_sha}")

    async def on_stop(self) -> None:
        pass

    async def sense(self) -> list[IncidentEvent]:
        current = self._head_sha()
        if current is None:
            logger.warning("[GitAgent] Could not read HEAD SHA")
            return []

        if self._last_sha is None:
            self._last_sha = current
            return []

        if current == self._last_sha:
            return []

        logger.info(f"[GitAgent] New commit: {self._last_sha[:8]} -> {current[:8]}")
        info = self._commit_info(current)
        events = await self._validate_commit(self._last_sha, current, info)
        self._last_sha = current
        return events

    def run_hook_validation(self) -> dict:
        return self.validator.validate()
