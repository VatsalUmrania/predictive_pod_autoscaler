"""
Git Commit Validation Steps (Deploy event, secrets, env contract, selfheal)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from nexus.agents.git.env_contract import EnvContractValidator
from nexus.agents.git.secrets import scan_diff_for_secrets
from nexus.bus.incident_event import (
    AgentType,
    DeployEventContext,
    EnvContractViolationContext,
    IncidentEvent,
    Severity,
    SignalType,
)

logger = logging.getLogger(__name__)


async def validate_commit(
    validator: EnvContractValidator,
    repo_path: Path,
    deployment_name: str,
    k8s_namespace: str,
    old_sha: str,
    new_sha: str,
    info: dict,
    diff: str,
    changed_files: list[str],
) -> list[IncidentEvent]:
    events: list[IncidentEvent] = []

    # 1. DEPLOY_EVENT
    events.append(
        IncidentEvent(
            agent=AgentType.GIT,
            signal_type=SignalType.DEPLOY_EVENT,
            severity=Severity.INFO,
            namespace=k8s_namespace,
            resource_name=deployment_name,
            resource_kind="Deployment",
            deploy_sha=new_sha,
            context=DeployEventContext(
                sha=new_sha,
                branch=info.get("refs", "unknown"),
                author=info.get("author", "unknown"),
                deployment_name=deployment_name,
                namespace=k8s_namespace,
                previous_sha=old_sha,
                changed_files=changed_files[:50],
            ).model_dump(),
        )
    )

    # 2. Secret scan
    findings = scan_diff_for_secrets(diff)
    if findings:
        logger.warning(
            f"[GitAgent] SECRET detected in {new_sha[:8]} — "
            f"labels: {[f['label'] for f in findings]}"
        )
        events.append(
            IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.SECRET_COMMITTED,
                severity=Severity.CRITICAL,
                namespace=k8s_namespace,
                resource_name=deployment_name,
                deploy_sha=new_sha,
                context={
                    "sha": new_sha,
                    "author": info.get("author"),
                    "findings": findings[:10],
                    "total_findings": len(findings),
                },
            )
        )

    # 3. .env contract validation
    loop = asyncio.get_running_loop()
    validation = await loop.run_in_executor(None, validator.validate)

    if not validation["passed"]:
        missing = validation["missing_keys"]
        logger.warning(
            f"[GitAgent] ENV contract FAIL: {len(missing)} missing keys: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
        events.append(
            IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.ENV_CONTRACT_VIOLATION,
                severity=Severity.CRITICAL,
                namespace=k8s_namespace,
                resource_name=deployment_name,
                resource_kind="Deployment",
                deploy_sha=new_sha,
                context=EnvContractViolationContext(
                    missing_keys=missing,
                    present_keys=validation["available_keys"][:50],
                    deployment_name=deployment_name,
                    namespace=k8s_namespace,
                ).model_dump(),
                suggested_healing_level=0,
                confidence=0.99,
            )
        )
        events.append(
            IncidentEvent(
                agent=AgentType.GIT,
                signal_type=SignalType.DEPLOY_BLOCKED,
                severity=Severity.WARNING,
                namespace=k8s_namespace,
                resource_name=deployment_name,
                deploy_sha=new_sha,
                context={
                    "reason": "env_contract_violation",
                    "missing_keys": missing,
                    "author": info.get("author"),
                },
            )
        )
    else:
        logger.info(
            f"[GitAgent] .env contract OK for {new_sha[:8]} — "
            f"{len(validation['required_keys'])} keys all present"
        )

    # 4. selfheal.yaml detection
    try:
        from nexus.integration.selfheal_config import load_selfheal_config

        sh_config = load_selfheal_config(repo_path)
        if sh_config:
            from nexus.integration.token_store import get_token_store

            store = get_token_store()
            token = await store.register_app(sh_config.app, tier=sh_config.tier)
            logger.info(
                f"[GitAgent] selfheal.yaml found for app='{sh_config.app}' token={token[:8]}…"
            )
            try:
                from nexus.integration.dashboard import cache_policy

                cache_policy(sh_config.app, sh_config.to_dict())
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f"[GitAgent] selfheal.yaml processing skipped: {exc}")

    return events
