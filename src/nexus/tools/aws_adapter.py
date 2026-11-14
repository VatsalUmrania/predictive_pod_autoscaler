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
    DynamoDBDescribeTableInput,
    DynamoDBDescribeTableSchema,
    DynamoDBDescribeTableTool,
    DynamoDBUpdateTableInput,
    DynamoDBUpdateTableSchema,
    DynamoDBUpdateTableTool,
    RDSDeleteInstanceInput,
    RDSDeleteInstanceSchema,
    RDSDeleteInstanceTool,
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
    "DynamoDBUpdateTableSchema",
    "DynamoDBUpdateTableInput",
    "DynamoDBDescribeTableSchema",
    "DynamoDBDescribeTableInput",
    "RDSDeleteInstanceSchema",
    "RDSDeleteInstanceInput",
    "AWSGetMetricDataTool",
    "AWSGetLogEventsTool",
    "AWSUpdateLambdaMemoryTool",
    "AWSUpdateLambdaTimeoutTool",
    "AWSRollbackLambdaAliasTool",
    "AWSReplaySQSDLQTool",
    "DynamoDBDescribeTableTool",
    "DynamoDBUpdateTableTool",
    "RDSDeleteInstanceTool",
]
