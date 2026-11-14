"""
AWS Tools Package
"""

from nexus.tools.aws.client import get_boto3_client
from nexus.tools.aws.schemas import (
    AWSGetLogEventsSchema,
    AWSGetMetricDataSchema,
    AWSReplaySQSDLQSchema,
    AWSRollbackLambdaAliasSchema,
    AWSUpdateLambdaMemorySchema,
    AWSUpdateLambdaTimeoutSchema,
    DynamoDBDescribeTableInput,
    DynamoDBDescribeTableSchema,
    DynamoDBUpdateTableInput,
    DynamoDBUpdateTableSchema,
    RDSDeleteInstanceInput,
    RDSDeleteInstanceSchema,
)
from nexus.tools.aws.tools import (
    AWSGetLogEventsTool,
    AWSGetMetricDataTool,
    AWSReplaySQSDLQTool,
    AWSRollbackLambdaAliasTool,
    AWSUpdateLambdaMemoryTool,
    AWSUpdateLambdaTimeoutTool,
    DynamoDBDescribeTableTool,
    DynamoDBUpdateTableTool,
    RDSDeleteInstanceTool,
)

__all__ = [
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
