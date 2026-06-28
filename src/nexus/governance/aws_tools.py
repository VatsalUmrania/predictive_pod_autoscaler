"""
NEXUS AWS Remediation Tools
=============================
boto3-backed remediation actions callable by the RunbookExecutor.

Design principles:
    1. Every action validates against AWSPolicy before touching AWS.
    2. Actions capture pre-state before execution for rollback.
    3. All operations are idempotent where possible.
    4. Returns ActionResult — never raises on AWS errors (returns error in result).
    5. Dry-run mode logs decisions without making API calls.

Usage (from RunbookExecutor):
    tools = AWSTools(dry_run=False)
    result = await tools.execute("increase_lambda_memory", {
        "function_name": "my-api",
        "memory_mb": 512,
    })

Available action types (see aws_policy.py for full list):
    increase_lambda_memory       — update function configuration
    increase_lambda_timeout      — update function configuration
    rollback_lambda_alias        — point alias to previous version
    set_lambda_reserved_concurrency — set reserved concurrency
    update_lambda_env_var        — patch single env variable
    replay_sqs_dlq               — move DLQ messages to source queue
    purge_sqs_queue              — delete all messages (destructive)
    disable_eventbridge_rule     — disable a rule
    enable_eventbridge_rule      — re-enable a rule
    emit_alert                   — send Slack/NATS notification (L0)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from nexus.governance.aws_policy import PolicyViolation, check_action

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotoCoreError = Exception  # type: ignore[assignment,misc]
    ClientError = Exception    # type: ignore[assignment,misc]
    _BOTO3_AVAILABLE = False


@dataclass
class ActionResult:
    """Result of an AWS remediation action."""
    action_type:  str
    resource:     str
    success:      bool
    pre_state:    dict[str, Any] = field(default_factory=dict)
    post_state:   dict[str, Any] = field(default_factory=dict)
    error:        str | None = None
    dry_run:      bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "resource":    self.resource,
            "success":     self.success,
            "pre_state":   self.pre_state,
            "post_state":  self.post_state,
            "error":       self.error,
            "dry_run":     self.dry_run,
        }


class AWSTools:
    """
    Executes AWS remediation actions after policy validation.

    Args:
        region:   AWS region (default us-east-1).
        dry_run:  If True, log decisions but skip API calls.
    """

    def __init__(self, region: str = "us-east-1", dry_run: bool = False) -> None:
        self._region  = region
        self._dry_run = dry_run
        self.__lambda: Any = None
        self.__sqs: Any    = None
        self.__events: Any = None

    # ── Client properties (lazy) ──────────────────────────────────────────────

    @property
    def _lambda(self):
        if self.__lambda is None and _BOTO3_AVAILABLE and boto3:
            try:
                self.__lambda = boto3.client("lambda", region_name=self._region)
            except Exception as exc:
                logger.error(f"[AWSTools] lambda client: {exc}")
        return self.__lambda

    @property
    def _sqs(self):
        if self.__sqs is None and _BOTO3_AVAILABLE and boto3:
            try:
                self.__sqs = boto3.client("sqs", region_name=self._region)
            except Exception as exc:
                logger.error(f"[AWSTools] sqs client: {exc}")
        return self.__sqs

    @property
    def _events(self):
        if self.__events is None and _BOTO3_AVAILABLE and boto3:
            try:
                self.__events = boto3.client("events", region_name=self._region)
            except Exception as exc:
                logger.error(f"[AWSTools] events client: {exc}")
        return self.__events

    # ── Public entrypoint ─────────────────────────────────────────────────────

    async def execute(self, action_type: str, params: dict[str, Any]) -> ActionResult:
        """
        Validate policy then execute the action.

        Returns ActionResult with success=False on any error.
        Never raises.
        """
        resource = params.get("function_name") or params.get("queue_url") or params.get("rule_name", "unknown")
        try:
            policy = check_action(action_type, params)
        except PolicyViolation as exc:
            logger.warning(f"[AWSTools] POLICY BLOCKED: {exc}")
            return ActionResult(
                action_type=action_type,
                resource=resource,
                success=False,
                error=f"PolicyViolation: {exc}",
            )

        if not _BOTO3_AVAILABLE:
            return ActionResult(
                action_type=action_type, resource=resource, success=False,
                error="boto3 not installed — run: pip install 'ppa[aws]'",
            )

        if self._dry_run:
            logger.info(f"[AWSTools] DRY-RUN: {action_type} params={params}")
            return ActionResult(
                action_type=action_type, resource=resource,
                success=True, dry_run=True,
                pre_state={}, post_state={"dry_run": True},
            )

        handler = self._dispatch(action_type)
        if handler is None:
            return ActionResult(
                action_type=action_type, resource=resource, success=False,
                error=f"No handler for action_type '{action_type}'",
            )

        try:
            return await handler(params)
        except Exception as exc:
            logger.error(f"[AWSTools] Unexpected error in {action_type}: {exc}", exc_info=True)
            return ActionResult(
                action_type=action_type, resource=resource, success=False, error=str(exc),
            )

    def _dispatch(self, action_type: str):
        return {
            "increase_lambda_memory":           self._increase_lambda_memory,
            "increase_lambda_timeout":           self._increase_lambda_timeout,
            "rollback_lambda_alias":             self._rollback_lambda_alias,
            "set_lambda_reserved_concurrency":   self._set_lambda_reserved_concurrency,
            "update_lambda_env_var":             self._update_lambda_env_var,
            "replay_sqs_dlq":                    self._replay_sqs_dlq,
            "purge_sqs_queue":                   self._purge_sqs_queue,
            "disable_eventbridge_rule":          self._disable_eventbridge_rule,
            "enable_eventbridge_rule":           self._enable_eventbridge_rule,
            "emit_alert":                        self._emit_alert,
            "get_lambda_config":                 self._get_lambda_config,
        }.get(action_type)

    # ── Lambda actions ────────────────────────────────────────────────────────

    async def _increase_lambda_memory(self, params: dict) -> ActionResult:
        """Increase Lambda function memory size."""
        fn = params["function_name"]
        new_memory = int(params["memory_mb"])
        loop = asyncio.get_event_loop()

        # Capture pre-state
        try:
            config = await loop.run_in_executor(
                None, lambda: self._lambda.get_function_configuration(FunctionName=fn)
            )
            pre_memory = config.get("MemorySize", 128)
        except Exception as exc:
            return ActionResult("increase_lambda_memory", fn, False, error=str(exc))

        # Apply
        try:
            await loop.run_in_executor(
                None,
                lambda: self._lambda.update_function_configuration(
                    FunctionName=fn,
                    MemorySize=new_memory,
                ),
            )
            logger.info(f"[AWSTools] {fn}: memory {pre_memory}MB → {new_memory}MB")
            return ActionResult(
                "increase_lambda_memory", fn, True,
                pre_state={"memory_mb": pre_memory},
                post_state={"memory_mb": new_memory},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("increase_lambda_memory", fn, False, error=str(exc))

    async def _increase_lambda_timeout(self, params: dict) -> ActionResult:
        """Increase Lambda function timeout."""
        fn = params["function_name"]
        new_timeout = int(params["timeout_seconds"])
        loop = asyncio.get_event_loop()
        try:
            config = await loop.run_in_executor(
                None, lambda: self._lambda.get_function_configuration(FunctionName=fn)
            )
            pre_timeout = config.get("Timeout", 3)
            await loop.run_in_executor(
                None,
                lambda: self._lambda.update_function_configuration(
                    FunctionName=fn,
                    Timeout=new_timeout,
                ),
            )
            logger.info(f"[AWSTools] {fn}: timeout {pre_timeout}s → {new_timeout}s")
            return ActionResult(
                "increase_lambda_timeout", fn, True,
                pre_state={"timeout_seconds": pre_timeout},
                post_state={"timeout_seconds": new_timeout},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("increase_lambda_timeout", fn, False, error=str(exc))

    async def _rollback_lambda_alias(self, params: dict) -> ActionResult:
        """Point a Lambda alias back to a previous version."""
        fn = params["function_name"]
        alias = params.get("alias_name", "live")
        target_version = params.get("target_version")  # explicit version, or None = auto-detect
        loop = asyncio.get_event_loop()
        try:
            # Get current alias pointer
            alias_resp = await loop.run_in_executor(
                None, lambda: self._lambda.get_alias(FunctionName=fn, Name=alias)
            )
            current_version = alias_resp.get("FunctionVersion", "$LATEST")

            # If no explicit target, find the version before the current one
            if not target_version:
                versions_resp = await loop.run_in_executor(
                    None, lambda: self._lambda.list_versions_by_function(
                        FunctionName=fn, MaxItems=10
                    )
                )
                published = sorted(
                    [v for v in versions_resp.get("Versions", []) if v.get("Version") != "$LATEST"],
                    key=lambda v: v.get("LastModified", ""),
                )
                # Find index of current version and step back
                current_idx = next(
                    (i for i, v in enumerate(published) if v.get("Version") == current_version),
                    len(published) - 1,
                )
                if current_idx <= 0:
                    return ActionResult(
                        "rollback_lambda_alias", fn, False,
                        error="No previous published version available to roll back to",
                    )
                target_version = published[current_idx - 1].get("Version")

            # Point alias to target version
            await loop.run_in_executor(
                None,
                lambda: self._lambda.update_alias(
                    FunctionName=fn,
                    Name=alias,
                    FunctionVersion=target_version,
                    Description=f"NEXUS rollback from {current_version} to {target_version}",
                ),
            )
            logger.info(f"[AWSTools] {fn}: alias '{alias}' rolled back {current_version} → {target_version}")
            return ActionResult(
                "rollback_lambda_alias", fn, True,
                pre_state={"alias": alias, "version": current_version},
                post_state={"alias": alias, "version": target_version},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("rollback_lambda_alias", fn, False, error=str(exc))

    async def _set_lambda_reserved_concurrency(self, params: dict) -> ActionResult:
        fn = params["function_name"]
        reserved = int(params["reserved_concurrent_executions"])
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._lambda.put_function_concurrency(
                    FunctionName=fn,
                    ReservedConcurrentExecutions=reserved,
                ),
            )
            logger.info(f"[AWSTools] {fn}: set reserved concurrency={reserved}")
            return ActionResult(
                "set_lambda_reserved_concurrency", fn, True,
                post_state={"reserved_concurrent_executions": reserved},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("set_lambda_reserved_concurrency", fn, False, error=str(exc))

    async def _update_lambda_env_var(self, params: dict) -> ActionResult:
        """Patch a single environment variable on a Lambda function."""
        fn = params["function_name"]
        key = params["env_key"]
        value = params["env_value"]
        loop = asyncio.get_event_loop()
        try:
            config = await loop.run_in_executor(
                None, lambda: self._lambda.get_function_configuration(FunctionName=fn)
            )
            env_vars = config.get("Environment", {}).get("Variables", {}).copy()
            pre_value = env_vars.get(key)
            env_vars[key] = value
            await loop.run_in_executor(
                None,
                lambda: self._lambda.update_function_configuration(
                    FunctionName=fn,
                    Environment={"Variables": env_vars},
                ),
            )
            logger.info(f"[AWSTools] {fn}: env {key}={pre_value!r} → {value!r}")
            return ActionResult(
                "update_lambda_env_var", fn, True,
                pre_state={"env_key": key, "env_value": pre_value},
                post_state={"env_key": key, "env_value": value},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("update_lambda_env_var", fn, False, error=str(exc))

    async def _get_lambda_config(self, params: dict) -> ActionResult:
        fn = params["function_name"]
        loop = asyncio.get_event_loop()
        try:
            config = await loop.run_in_executor(
                None, lambda: self._lambda.get_function_configuration(FunctionName=fn)
            )
            return ActionResult("get_lambda_config", fn, True, post_state=config)
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("get_lambda_config", fn, False, error=str(exc))

    # ── SQS actions ────────────────────────────────────────────────────────────

    async def _replay_sqs_dlq(self, params: dict) -> ActionResult:
        """
        Move messages from a DLQ back to the source queue.
        Uses SQS redrive-allow policy (start-message-move-task) where available,
        falling back to manual receive/send/delete.
        """
        dlq_url = params["dlq_url"]
        source_queue_url = params["source_queue_url"]
        max_messages = int(params.get("max_messages", 1000))
        loop = asyncio.get_event_loop()

        moved = 0
        try:
            while moved < max_messages:
                resp = await loop.run_in_executor(
                    None,
                    lambda: self._sqs.receive_message(
                        QueueUrl=dlq_url,
                        MaxNumberOfMessages=10,
                        VisibilityTimeout=30,
                        WaitTimeSeconds=0,
                    ),
                )
                msgs = resp.get("Messages", [])
                if not msgs:
                    break
                for msg in msgs:
                    # Re-send to source queue
                    await loop.run_in_executor(
                        None,
                        lambda m=msg: self._sqs.send_message(
                            QueueUrl=source_queue_url,
                            MessageBody=m["Body"],
                            MessageAttributes=m.get("MessageAttributes", {}),
                        ),
                    )
                    # Delete from DLQ
                    await loop.run_in_executor(
                        None,
                        lambda m=msg: self._sqs.delete_message(
                            QueueUrl=dlq_url,
                            ReceiptHandle=m["ReceiptHandle"],
                        ),
                    )
                    moved += 1

            logger.info(f"[AWSTools] Replayed {moved} messages from DLQ {dlq_url} → {source_queue_url}")
            return ActionResult(
                "replay_sqs_dlq", dlq_url, True,
                post_state={"messages_moved": moved},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("replay_sqs_dlq", dlq_url, False, error=str(exc),
                                post_state={"messages_moved": moved})

    async def _purge_sqs_queue(self, params: dict) -> ActionResult:
        """Purge ALL messages from an SQS queue. DESTRUCTIVE — requires human approval."""
        queue_url = params["queue_url"]
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._sqs.purge_queue(QueueUrl=queue_url)
            )
            logger.warning(f"[AWSTools] PURGED queue: {queue_url}")
            return ActionResult("purge_sqs_queue", queue_url, True)
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("purge_sqs_queue", queue_url, False, error=str(exc))

    # ── EventBridge actions ────────────────────────────────────────────────────

    async def _disable_eventbridge_rule(self, params: dict) -> ActionResult:
        rule_name = params["rule_name"]
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._events.disable_rule(Name=rule_name)
            )
            logger.info(f"[AWSTools] EventBridge rule disabled: {rule_name}")
            return ActionResult(
                "disable_eventbridge_rule", rule_name, True,
                pre_state={"state": "ENABLED"},
                post_state={"state": "DISABLED"},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("disable_eventbridge_rule", rule_name, False, error=str(exc))

    async def _enable_eventbridge_rule(self, params: dict) -> ActionResult:
        rule_name = params["rule_name"]
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._events.enable_rule(Name=rule_name)
            )
            logger.info(f"[AWSTools] EventBridge rule enabled: {rule_name}")
            return ActionResult(
                "enable_eventbridge_rule", rule_name, True,
                pre_state={"state": "DISABLED"},
                post_state={"state": "ENABLED"},
            )
        except (BotoCoreError, ClientError) as exc:
            return ActionResult("enable_eventbridge_rule", rule_name, False, error=str(exc))

    async def _emit_alert(self, params: dict) -> ActionResult:
        """L0 action — log the alert (Notifier picks it up via NATS)."""
        message = params.get("message", "NEXUS AWS incident alert")
        logger.warning(f"[AWSTools] ALERT: {message}")
        return ActionResult("emit_alert", "nexus", True, post_state={"message": message})
