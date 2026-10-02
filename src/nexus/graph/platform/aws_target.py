"""
AWS target detection and handling predicates.
"""

from __future__ import annotations

from typing import Any

from nexus.graph.platform.base import TargetResource


def can_handle_aws(event_or_target: dict[str, Any] | TargetResource) -> bool:
    """Check if the event or target belongs to AWS."""
    if isinstance(event_or_target, TargetResource):
        return event_or_target.platform.lower() == "aws"

    if isinstance(event_or_target, dict):
        plat = str(event_or_target.get("platform", "")).lower()
        if plat == "aws":
            return True
        agent = str(event_or_target.get("agent", "")).lower()
        if agent in ("lambda", "apigw", "sqs", "dynamodb", "cloudwatch"):
            return True
        res_name = str(event_or_target.get("resource_name", ""))
        if res_name.startswith("arn:aws:"):
            return True
        if "aws" in str(event_or_target.get("namespace", "")).lower():
            return True

    return False


def detect_aws_target(events: list[dict[str, Any]]) -> TargetResource:
    """Detect the AWS target resource from trigger events."""
    if not events:
        return TargetResource(platform="aws", namespace="us-east-1", name="unknown", kind="lambda")

    primary_evt = events[0]
    for evt in events:
        if str(evt.get("severity", "")).lower() == "critical":
            primary_evt = evt
            break

    res_name = str(primary_evt.get("resource_name", "unknown"))
    agent = str(primary_evt.get("agent", "")).lower()
    region = str(primary_evt.get("namespace", "us-east-1"))
    kind = "lambda"

    if res_name.startswith("arn:aws:"):
        parts = res_name.split(":")
        if len(parts) >= 6:
            kind = parts[2]
            if parts[3]:
                region = parts[3]
            if len(parts) >= 7:
                res_name = parts[6]
            else:
                raw_tail = parts[5]
                res_name = raw_tail.split("/")[-1] if "/" in raw_tail else raw_tail
    elif agent in ("lambda", "sqs", "dynamodb", "cloudwatch", "ecs", "apigw"):
        kind = agent

    return TargetResource(
        platform="aws",
        namespace=region or "us-east-1",
        name=res_name,
        kind=kind,
        arn_or_uri=primary_evt.get("resource_name") if str(primary_evt.get("resource_name", "")).startswith("arn:aws:") else None,
    )
