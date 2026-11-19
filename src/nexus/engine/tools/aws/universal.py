"""
Universal AWS Provider Tool Bridge
==================================
Schema-validated, service-agnostic Boto3 inspection, telemetry, and mutation
bridge supporting any AWS cloud service (Lambda, RDS, ECS, SQS, DynamoDB, etc.).
"""

from __future__ import annotations

import json
import time
from typing import Any, Literal

import boto3
from botocore.exceptions import ClientError, ParamValidationError
from pydantic import BaseModel, Field

ALLOWED_READ_PREFIXES = (
    "describe_",
    "list_",
    "get_",
    "filter_",
    "lookup_",
    "batch_get_",
)
DENIED_READ_OPERATIONS = frozenset(
    {
        "get_secret_value",
        "get_parameter",
        "get_parameters",
        "get_parameters_by_path",
        "get_federation_token",
        "get_session_token",
    }
)
DENIED_MUTATE_SERVICES = frozenset(
    {"iam", "kms", "organizations", "sts", "billing", "ce"}
)


class AWSInspectInput(BaseModel):
    service: str = Field(
        description="Boto3 service name, e.g. lambda, rds, ecs, sqs, dynamodb, ec2"
    )
    operation: str = Field(
        description="Read-only boto3 method starting with describe_, list_, get_, filter_, lookup_"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Kwargs for operation"
    )
    region: str = Field(default="us-east-1", description="AWS region")


class AWSQueryTelemetryInput(BaseModel):
    telemetry_type: Literal[
        "cloudwatch_metrics", "cloudwatch_logs_insights", "cloudtrail_events"
    ]
    region: str = Field(default="us-east-1")
    parameters: dict[str, Any] = Field(
        description="Kwargs for metrics, log insights query, or CloudTrail lookup_events"
    )


class AWSMutateInput(BaseModel):
    service: str = Field(
        description="Boto3 service name, e.g. lambda, rds, ecs, dynamodb, sqs"
    )
    operation: str = Field(description="Mutating boto3 method name")
    parameters: dict[str, Any] = Field(
        description="Exact boto3 kwargs for the mutation"
    )
    target_arn: str = Field(description="Target AWS resource ARN or identifier")
    region: str = Field(default="us-east-1")


def execute_aws_inspect(args: AWSInspectInput) -> str:
    """Execute a read-only Boto3 inspection operation with self-correction feedback."""
    op = args.operation.lower()
    if not op.startswith(ALLOWED_READ_PREFIXES) or op in DENIED_READ_OPERATIONS:
        return json.dumps(
            {
                "error": f"Operation '{args.operation}' is not a permitted read-only inspection method.",
                "allowed_prefixes": ALLOWED_READ_PREFIXES,
            }
        )
    try:
        client = boto3.client(args.service, region_name=args.region)
        if not hasattr(client, args.operation):
            available = [m for m in dir(client) if m.startswith(ALLOWED_READ_PREFIXES)][
                :40
            ]
            return json.dumps(
                {
                    "error": f"Service '{args.service}' has no operation '{args.operation}'.",
                    "hint_available_read_operations": available,
                }
            )
        method = getattr(client, args.operation)
        response = method(**args.parameters)
        if isinstance(response, dict):
            response.pop("ResponseMetadata", None)
        return json.dumps(response, default=str)[:12000]
    except (ParamValidationError, ClientError, TypeError) as exc:
        return json.dumps(
            {
                "error": str(exc),
                "self_correction_hint": f"Check parameter names and types for {args.service}.{args.operation} and retry.",
            }
        )


def execute_aws_query_telemetry(args: AWSQueryTelemetryInput) -> str:
    """Query CloudWatch Metrics, CloudWatch Logs Insights, or CloudTrail Events."""
    try:
        if args.telemetry_type == "cloudwatch_metrics":
            cw = boto3.client("cloudwatch", region_name=args.region)
            op = (
                "get_metric_data"
                if "MetricDataQueries" in args.parameters
                else "get_metric_statistics"
            )
            res = getattr(cw, op)(**args.parameters)
            if isinstance(res, dict):
                res.pop("ResponseMetadata", None)
            return json.dumps(res, default=str)[:12000]

        if args.telemetry_type == "cloudwatch_logs_insights":
            logs = boto3.client("logs", region_name=args.region)
            start_res = logs.start_query(**args.parameters)
            query_id = start_res["queryId"]
            for _ in range(10):
                res = logs.get_query_results(queryId=query_id)
                if res.get("status") in ("Complete", "Failed", "Cancelled"):
                    return json.dumps(
                        {
                            "status": res.get("status"),
                            "results": res.get("results", [])[:50],
                        },
                        default=str,
                    )
                time.sleep(0.5)
            return json.dumps({"status": "Running", "queryId": query_id})

        if args.telemetry_type == "cloudtrail_events":
            ct = boto3.client("cloudtrail", region_name=args.region)
            res = ct.lookup_events(**args.parameters)
            if isinstance(res, dict):
                res.pop("ResponseMetadata", None)
            return json.dumps(res, default=str)[:12000]

        return json.dumps(
            {"error": f"Unsupported telemetry_type: {args.telemetry_type}"}
        )
    except (ParamValidationError, ClientError, TypeError) as exc:
        return json.dumps({"error": str(exc)})


def execute_aws_mutate(args: AWSMutateInput) -> str:
    """Execute mutating operations on AWS infrastructure with strict perimeter security."""
    if args.service.lower() in DENIED_MUTATE_SERVICES:
        raise PermissionError(
            f"Mutations on security/account service '{args.service}' are strictly prohibited."
        )
    client = boto3.client(args.service, region_name=args.region)
    method = getattr(client, args.operation)
    response = method(**args.parameters)
    if isinstance(response, dict):
        response.pop("ResponseMetadata", None)
    return json.dumps(response, default=str)[:8000]
