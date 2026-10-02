"""
NEXUS AWS Tools Adapter Facade
==============================
Backward-compatibility facade for AWS tools and schemas.
"""

from nexus.tools.aws import (
    AWSGetLogEventsSchema,
    AWSGetLogEventsTool,
    AWSGetMetricDataSchema,
    AWSGetMetricDataTool,
    AWSReplaySQSDLQSchema,
    AWSReplaySQSDLQTool,
    AWSRollbackLambdaAliasSchema,
    AWSRollbackLambdaAliasTool,
    AWSUpdateLambdaMemorySchema,
    AWSUpdateLambdaMemoryTool,
    AWSUpdateLambdaTimeoutSchema,
    AWSUpdateLambdaTimeoutTool,
    get_boto3_client,
)

_get_boto3_client = get_boto3_client

__all__ = [
    "_get_boto3_client",
    "get_boto3_client",
    "AWSGetMetricDataSchema",
    "AWSGetLogEventsSchema",
    "AWSUpdateLambdaMemorySchema",
    "AWSUpdateLambdaTimeoutSchema",
    "AWSRollbackLambdaAliasSchema",
    "AWSReplaySQSDLQSchema",
    "AWSGetMetricDataTool",
    "AWSGetLogEventsTool",
    "AWSUpdateLambdaMemoryTool",
    "AWSUpdateLambdaTimeoutTool",
    "AWSRollbackLambdaAliasTool",
    "AWSReplaySQSDLQTool",
]
